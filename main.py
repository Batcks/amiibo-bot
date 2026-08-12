import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import json
import os
from datetime import datetime
from flask import Flask
from threading import Thread
import time

TOKEN = os.getenv('DISCORD_TOKEN')
URL_GAME = 'https://www.game.es/buscar/amiibo'
URL_API = 'https://www.game.es/api/search'
ARCHIVO_COLECCION = 'mi_coleccion.json'
ARCHIVO_ULTIMO_CATALOGO = 'ultimo_catalogo.json'
UMBRAL_ALERTA = 14.99
PRECIO_POR_DEFECTO = 15.00  # cuando la API no da un precio válido para un producto

# --- CATEGORÍAS DE AMIIBOS ---
# Orden de prioridad (de arriba a abajo) Y de visualización. Un producto cae
# en la primera categoría cuya palabra clave aparezca en su nombre; si no
# coincide con ninguna, va a "otros". Para añadir una categoría nueva, solo
# hay que añadir una tupla aquí (clave, etiqueta visible, palabra a buscar).
CATEGORIAS = [
    ("smash", "Smash", "smash"),
    ("mario", "Super Mario", "mario"),
]
CLAVE_OTROS = "otros"
ETIQUETA_OTROS = "Otros"
CLAVES_CATEGORIAS = [clave for clave, _, _ in CATEGORIAS] + [CLAVE_OTROS]
ETIQUETAS_CATEGORIAS = {clave: etiqueta for clave, etiqueta, _ in CATEGORIAS}
ETIQUETAS_CATEGORIAS[CLAVE_OTROS] = ETIQUETA_OTROS
CATEGORIA_CON_ALERTA = "smash"  # única categoría en la que se muestra 🚨 por precio bajo
CATEGORIA_COLECCIONABLE = "smash"  # única categoría en la que aplica /añadir, /quitar y el tachado

def determinar_categoria(nombre_lower):
    for clave, _, palabra in CATEGORIAS:
        if palabra in nombre_lower:
            return clave
    return CLAVE_OTROS
# ------------------------------

# --- VIGILANCIA PERIÓDICA DEL CATÁLOGO ---
INTERVALO_MINUTOS = 60  # cada cuánto se comprueba si ha cambiado el catálogo
ARCHIVO_VIGILANCIA_CONFIG = 'vigilancia_config.json'  # qué servidores/canales tienen la vigilancia activada
# -------------------------------------------

# Evita que una comprobación manual (/catalogo) y la automática se pisen
# entre sí, y que se disparen dos tandas de peticiones a la API a la vez.
catalogo_lock = asyncio.Lock()

intents = discord.Intents.default()
# Ya no hace falta message_content: todos los comandos son slash commands
# (bot.tree), no comandos por prefijo. Si en el futuro añades comandos con
# @bot.command, vuelve a activarlo (y en el portal de Discord Developer).
bot = commands.Bot(command_prefix='!', intents=intents)

# --- CONFIGURACIÓN DEL SERVIDOR WEB ---
app = Flask('')

@app.route('/')
def home():
    return "Bot activo"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
# --------------------------------------

def cargar_json(ruta, valor_por_defecto):
    if os.path.exists(ruta):
        with open(ruta, 'r', encoding='utf-8') as f:
            return json.load(f)
    return valor_por_defecto

def guardar_json(ruta, datos):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=4)

def cargar_coleccion():
    datos = cargar_json(ARCHIVO_COLECCION, None)

    if datos is None:
        return []

    if isinstance(datos, dict):
        # Formato con categorías (de una versión anterior del bot). Se
        # recupera solo la parte "smash", que es la única coleccionable
        # ahora, y se guarda ya como lista plana.
        migrado = datos.get("smash", [])
        guardar_coleccion(migrado)
        print(f"[coleccion] migrado mi_coleccion.json de dict por categoría a lista plana ({len(migrado)} amiibo(s) de smash)", flush=True)
        return migrado

    return datos

def guardar_coleccion(coleccion):
    guardar_json(ARCHIVO_COLECCION, coleccion)

def cargar_ultimo_catalogo():
    return cargar_json(ARCHIVO_ULTIMO_CATALOGO, {})

def guardar_ultimo_catalogo(catalogo):
    guardar_json(ARCHIVO_ULTIMO_CATALOGO, catalogo)

def cargar_config_vigilancia():
    return cargar_json(ARCHIVO_VIGILANCIA_CONFIG, {})  # {"<guild_id>": <channel_id>, ...}

def guardar_config_vigilancia(config):
    guardar_json(ARCHIVO_VIGILANCIA_CONFIG, config)

def formatear_link(link):
    if not link:
        return None
    if link.startswith('/'):
        return f"https://www.game.es{link}"
    return link


def formatear_item(nombre, precio_str, lo_tengo, link=None, precio_num=None, mostrar_alerta=False):
    """Genera la línea de texto para un amiibo, ya sea normal o
    reacondicionado, tachado o no."""
    if lo_tengo:
        return f"**~~{nombre}~~** — {precio_str}€"

    alerta = ""
    if mostrar_alerta and precio_num is not None and precio_num < UMBRAL_ALERTA:
        alerta = " 🚨"

    link_completo = formatear_link(link)
    if link_completo:
        return f"{nombre} — {precio_str}€{alerta} ([Ver web]({link_completo}))"
    return f"{nombre} — {precio_str}€{alerta}"


def dividir_en_embeds(titulo, descripcion, color):
    """Divide una descripción larga en varios embeds si supera el límite de
    Discord (4096 caracteres por embed), cortando siempre por líneas
    completas (nunca a mitad de un amiibo)."""
    limite = 4096
    lineas = descripcion.split("\n")
    partes = []
    actual = ""
    for linea in lineas:
        candidato = f"{actual}\n{linea}" if actual else linea
        if len(candidato) > limite:
            if actual:
                partes.append(actual)
            actual = linea  # si una sola línea ya supera el límite, se envía tal cual
        else:
            actual = candidato
    if actual:
        partes.append(actual)
    if not partes:
        partes = [""]

    embeds = []
    for i, parte in enumerate(partes):
        titulo_parte = titulo if len(partes) == 1 else f"{titulo} ({i + 1}/{len(partes)})"
        embeds.append(discord.Embed(title=titulo_parte, description=parte, color=color))
    return embeds


def construir_secciones(categorias, tipo):
    """Construye los bloques de texto '**Etiqueta:**\\n...' para un tipo
    (normal/reacondicionado), en el orden de CLAVES_CATEGORIAS, omitiendo
    las categorías vacías."""
    secciones = []
    for clave in CLAVES_CATEGORIAS:
        lista = categorias[clave][tipo]
        if lista:
            secciones.append(f"**{ETIQUETAS_CATEGORIAS[clave]}:**\n" + "\n".join(lista))
    return secciones


# --- SESIÓN HTTP PERSISTENTE ---
# Reutilizada entre llamadas en vez de crear una aiohttp.ClientSession nueva
# en cada consulta: evita repetir el handshake TCP/TLS constantemente y
# mantiene las cookies de sesión de forma continua (más parecido a un
# navegador normal que abrir/cerrar sesión en cada ronda de vigilancia).
_sesion_http = None

async def obtener_sesion_http():
    global _sesion_http
    if _sesion_http is None or _sesion_http.closed:
        timeout = aiohttp.ClientTimeout(total=15)  # evita quedarse colgado si la API no responde
        _sesion_http = aiohttp.ClientSession(timeout=timeout)
    return _sesion_http

async def cerrar_sesion_http():
    """Fuerza que se cree una sesión nueva en la siguiente consulta (se usa
    cuando algo ha ido mal, para no quedarnos arrastrando una sesión rota)."""
    global _sesion_http
    if _sesion_http is not None:
        try:
            await _sesion_http.close()
        except Exception:
            pass
        _sesion_http = None
# --------------------------------


async def obtener_catalogo_api():
    url = URL_API
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
        'Content-Type': 'application/json; charset=UTF-8',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': URL_GAME
    }

    todos_los_productos = []
    pagina = 0
    total_resultados = None  # se rellena con lo que devuelva la propia API
    total_paginas = None

    session = await obtener_sesion_http()

    # 1. Llamada inicial: carga la página y establece las cookies de sesión
    #    que luego se envían automáticamente en los POST (cookie jar de aiohttp).
    try:
        resp_inicial = await session.get(URL_GAME, headers={'User-Agent': headers['User-Agent']})
        resp_inicial.release()
        if resp_inicial.status != 200:
            print(f"[catalogo] aviso: la carga inicial devolvió status {resp_inicial.status}", flush=True)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[catalogo] error en la petición inicial: {e}", flush=True)

    # 2. Bucle de paginación siguiendo el contrato real de la API:
    #    - Página 0: solo los campos base, FirstSearch siempre False.
    #    - Página 1+: se añaden CurrentProducts/HotProducts/TotalResults/
    #      TotalPages, reenviando los valores que la propia API devolvió
    #      en la respuesta anterior (no valores inventados).
    #    - Se para cuando ya se han acumulado TotalResults productos.
    while True:
        payload = {
            "MinPrice": None,
            "MaxPrice": None,
            "Head": "amiibo",
            "SKU": "",
            "Order": 7,
            "CategoryFilter": [],
            "Category": None,
            "TotalPages": None,
            "FirstSearch": False,
            "Page": pagina
        }

        if pagina > 0:
            payload["CurrentProducts"] = len(todos_los_productos)
            payload["HotProducts"] = 0
            payload["TotalResults"] = total_resultados
            payload["TotalPages"] = total_paginas

        try:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    print(f"[catalogo] página {pagina}: status {response.status}, se para la paginación", flush=True)
                    break
                datos = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            print(f"[catalogo] error en página {pagina}: {e}", flush=True)
            break

        productos_pagina = datos.get("Products", [])
        if not productos_pagina:
            break

        todos_los_productos.extend(productos_pagina)
        total_resultados = datos.get("TotalResults", total_resultados)
        total_paginas = datos.get("TotalPages", total_paginas)

        if total_resultados is not None and len(todos_los_productos) >= total_resultados:
            break

        pagina += 1
        if pagina > 10:  # límite de seguridad por si TotalResults no llega o es inconsistente
            print("[catalogo] aviso: se alcanzó el límite de seguridad de páginas", flush=True)
            break

        await asyncio.sleep(0.3)  # pequeña pausa para no golpear la API de golpe

    print(f"[catalogo] productos totales obtenidos: {len(todos_los_productos)} (API reporta TotalResults={total_resultados})", flush=True)

    # 3. Procesamos los datos extraídos
    coleccion = [c.lower() for c in cargar_coleccion()]
    catalogo_actual = {}

    # listas[clave]["normal" | "reacondicionado"] -> lista de líneas de texto
    listas = {clave: {"normal": [], "reacondicionado": []} for clave in CLAVES_CATEGORIAS}
    tengo_smash = 0
    no_tengo_smash = 0

    for producto in todos_los_productos:
        nombre = producto.get('Name')
        if not nombre:
            continue

        nombre_lower = nombre.lower()
        categoria = determinar_categoria(nombre_lower)
        es_reacondicionado = "reacondicionado" in nombre_lower
        # La colección (y por tanto el tachado) solo aplica a Smash.
        lo_tengo = categoria == CATEGORIA_COLECCIONABLE and any(item in nombre_lower for item in coleccion)

        link_parcial = producto.get('Navigation', '')
        link = f"https://www.game.es/{link_parcial}" if link_parcial else None

        ofertas = producto.get('Offers', [])
        precio_num = PRECIO_POR_DEFECTO
        if ofertas:
            try:
                precio_num = float(ofertas[0].get('SellPrice') or PRECIO_POR_DEFECTO)
            except (TypeError, ValueError):
                precio_num = PRECIO_POR_DEFECTO

        precio_str = f"{precio_num:.2f}".replace('.', ',')

        mostrar_alerta = categoria == CATEGORIA_CON_ALERTA and not es_reacondicionado

        texto = formatear_item(
            nombre, precio_str, lo_tengo,
            link=link, precio_num=precio_num, mostrar_alerta=mostrar_alerta
        )

        tipo = "reacondicionado" if es_reacondicionado else "normal"
        listas[categoria][tipo].append(texto)

        if not es_reacondicionado:
            catalogo_actual[nombre] = precio_str
            if categoria == CATEGORIA_COLECCIONABLE:
                if lo_tengo:
                    tengo_smash += 1
                else:
                    no_tengo_smash += 1

    lineas_resumen = []
    for clave in CLAVES_CATEGORIAS:
        etiqueta = ETIQUETAS_CATEGORIAS[clave]
        total = len(listas[clave]["normal"])
        reac = len(listas[clave]["reacondicionado"])
        if clave == CATEGORIA_COLECCIONABLE:
            lineas_resumen.append(
                f"📊 {etiqueta}: {total} (♻️ {reac}) | ✅ {tengo_smash} | ❌ {no_tengo_smash}"
            )
        else:
            lineas_resumen.append(f"📊 {etiqueta}: {total} (♻️ {reac})")

    resumen = "\n".join(lineas_resumen) + "\n\n"

    return {
        "resumen": resumen,
        "categorias": listas,
        "catalogo_actual": catalogo_actual,
    }


def detectar_cambios(catalogo_actual):
    """Compara catalogo_actual con el último catálogo guardado
    (ultimo_catalogo.json), actualiza el archivo y devuelve (nuevos,
    desaparecidos) como listas de texto 'nombre — precio€'.

    Esta es la ÚNICA función que lee/escribe ultimo_catalogo.json — ni
    /catalogo ni obtener_catalogo_api() lo tocan; solo /cambios y la
    vigilancia automática llaman a esto."""
    ultimo_catalogo = cargar_ultimo_catalogo()

    nuevos = []
    desaparecidos = []

    for nombre, precio in catalogo_actual.items():
        if nombre not in ultimo_catalogo:
            nuevos.append(f"{nombre} — {precio}€")

    for nombre, precio in ultimo_catalogo.items():
        if nombre not in catalogo_actual:
            desaparecidos.append(f"{nombre} — {precio}€")

    guardar_ultimo_catalogo(catalogo_actual)

    return nuevos, desaparecidos


@bot.tree.command(name='añadir', description='Añade un amiibo de Smash a tu colección')
async def añadir_coleccion(interaction: discord.Interaction, nombre: str):
    coleccion = cargar_coleccion()
    if nombre not in coleccion:
        coleccion.append(nombre)
        guardar_coleccion(coleccion)
        await interaction.response.send_message(f"Añadido a tu colección: **{nombre}**")
    else:
        await interaction.response.send_message(f"**{nombre}** ya estaba en tu colección.")

@bot.tree.command(name='quitar', description='Quita un amiibo de Smash de tu colección')
async def quitar_coleccion(interaction: discord.Interaction, nombre: str):
    coleccion = cargar_coleccion()
    encontrado = None
    for item in coleccion:
        if item.lower() == nombre.lower():
            encontrado = item
            break

    if encontrado:
        coleccion.remove(encontrado)
        guardar_coleccion(coleccion)
        await interaction.response.send_message(f"Eliminado de tu colección: **{encontrado}**")
    else:
        await interaction.response.send_message(f"No se encontró **{nombre}** en tu colección.")

@bot.tree.command(name='coleccion',description='Muestra la colección de amiibo')
async def mostrar_coleccion(interaction: discord.Interaction):
    coleccion = cargar_coleccion()

    if not coleccion:
        await interaction.response.send_message("Tu colección está vacía actualmente.")
        return

    # Usamos defer() si creemos que puede tardar, o simplemente respondemos
    await interaction.response.defer()

    mensaje = "**Tus Amiibos guardados:**\n"
    for item in coleccion:
        linea = f"• {item}\n"
        if len(mensaje) + len(linea) > 1900:
            await interaction.followup.send(mensaje)
            mensaje = linea
        else:
            mensaje += linea

    # En este punto mensaje siempre tiene contenido: si coleccion estuviera
    # vacía ya habríamos vuelto arriba (línea del "colección vacía").
    await interaction.followup.send(mensaje)

@bot.tree.command(name='ayuda',description='Muestra los comandos del bot y sus funciones')
async def mostrar_ayuda(interaction: discord.Interaction):
    texto_ayuda = (
        "**Comandos del Bot:**\n"
        "• `/catalogo` - Muestra el catálogo de Amiibos (Smash y otros) en un panel continuo.\n"
        "• `/cambios` - Comprueba manualmente si ha cambiado el catálogo desde la última vez.\n"
        "• `/vigilancia` - Activa/desactiva el aviso automático cuando cambie el catálogo.\n"
        "• `/coleccion` - Muestra tu colección de amiibos de Smash.\n"
        "• `/añadir [nombre]` - Añade un Amiibo de Smash a tu colección personal.\n"
        "• `/quitar [nombre]` - Elimina un Amiibo de Smash de tu colección personal.\n"
        "• `/ayuda` - Muestra este menú de ayuda."
    )
    await interaction.response.send_message(texto_ayuda)

@bot.tree.command(name='catalogo', description='Muestra el catálogo de Amiibos de GAME')
@app_commands.checks.cooldown(1, 15, key=lambda i: i.guild_id)
async def mostrar_catalogo(interaction: discord.Interaction):
    # 1. Mensaje inicial obligatorio
    await interaction.response.send_message("Abriendo catalogo...")
    tiempo_inicio = time.time()

    try:
        async with catalogo_lock:
            datos = await obtener_catalogo_api()

        tiempo_fin = time.time()
        resultado = round(tiempo_fin - tiempo_inicio, 1)

        categorias = datos["categorias"]
        hay_resultados = any(
            categorias[clave][tipo]
            for clave in CLAVES_CATEGORIAS
            for tipo in ("normal", "reacondicionado")
        )

        if not hay_resultados:
            await interaction.edit_original_response(content="No hay resultados de amiibo en la web.")
            return

        # 2. Modificamos el mensaje inicial de carga con el tiempo de respuesta
        await interaction.edit_original_response(content=f"**Tiempo de respuesta: {resultado}s**")

        # --- PRIMER EMBED (o varios si no cabe): normales, en orden de categorías ---
        secciones_normal = construir_secciones(categorias, "normal")
        descripcion_normal = datos["resumen"] + "\n\n".join(secciones_normal)

        # Para enviar los embeds extra usamos followup porque ya respondimos antes
        for embed in dividir_en_embeds("🛒 Catálogo de Amiibos", descripcion_normal, discord.Color.blue()):
            await interaction.followup.send(embed=embed)

        # --- SEGUNDO EMBED (o varios): reacondicionados, mismo orden de categorías ---
        secciones_reac = construir_secciones(categorias, "reacondicionado")
        if secciones_reac:
            descripcion_reac = "\n\n".join(secciones_reac)
            for embed in dividir_en_embeds("♻️ Reacondicionados", descripcion_reac, discord.Color.green()):
                await interaction.followup.send(embed=embed)

    except Exception as e:
        await cerrar_sesion_http()
        await interaction.edit_original_response(content=f"Ha ocurrido un error al cargar la web: {e}")

@mostrar_catalogo.error
async def mostrar_catalogo_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"Espera {error.retry_after:.0f}s antes de volver a pedir el catálogo.", ephemeral=True)
    else:
        raise error


def formatear_embeds_cambios(nuevos, desaparecidos):
    ahora = datetime.now().strftime('%d/%m/%Y %H:%M')

    descripcion = ""
    if nuevos:
        descripcion += "**:arrow_forward: Añadido:**\n" + "\n".join(nuevos) + "\n\n"
    if desaparecidos:
        descripcion += "**:arrow_backward: Removido:**\n" + "\n".join(desaparecidos)

    return dividir_en_embeds(f"**__Cambios {ahora}__**", descripcion.strip(), discord.Color.gold())


@bot.tree.command(name='cambios', description='Comprueba si ha cambiado el catálogo desde la última vez')
@app_commands.checks.cooldown(1, 15, key=lambda i: i.guild_id)
async def mostrar_cambios(interaction: discord.Interaction):
    await interaction.response.send_message("Comprobando cambios...")

    try:
        async with catalogo_lock:
            datos = await obtener_catalogo_api()
            nuevos, desaparecidos = detectar_cambios(datos["catalogo_actual"])

        if not (nuevos or desaparecidos):
            await interaction.edit_original_response(content="No hay cambios desde la última comprobación.")
            return

        await interaction.edit_original_response(
            content=f"Se han detectado cambios: {len(nuevos)} nuevo(s), {len(desaparecidos)} desaparecido(s)."
        )
        for embed in formatear_embeds_cambios(nuevos, desaparecidos):
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await cerrar_sesion_http()
        await interaction.edit_original_response(content=f"Ha ocurrido un error al comprobar cambios: {e}")

@mostrar_cambios.error
async def mostrar_cambios_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"Espera {error.retry_after:.0f}s antes de volver a comprobar cambios.", ephemeral=True)
    else:
        raise error


@bot.tree.command(name='vigilancia', description=f'Activa o desactiva el aviso automático de cambios en el catálogo (cada {INTERVALO_MINUTOS} min)')
async def alternar_vigilancia(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("Este comando solo se puede usar dentro de un servidor.", ephemeral=True)
        return

    config = cargar_config_vigilancia()
    guild_id = str(interaction.guild_id)

    if guild_id in config:
        del config[guild_id]
        guardar_config_vigilancia(config)
        await interaction.response.send_message("🛑 Vigilancia desactivada.")
    else:
        config[guild_id] = interaction.channel_id
        guardar_config_vigilancia(config)
        await interaction.response.send_message(
            f"✅ Vigilancia activada en este canal (revisión cada {INTERVALO_MINUTOS} min)."
        )


@tasks.loop(minutes=INTERVALO_MINUTOS)
async def vigilar_catalogo():
    config = cargar_config_vigilancia()
    if not config:
        return  # nadie tiene la vigilancia activada, no molestamos a la API

    if catalogo_lock.locked():
        # Ya hay una consulta en marcha (manual o automática); nos saltamos
        # esta ronda en vez de encolarnos y acumular retraso.
        print("[vigilancia] consulta ya en marcha, se salta esta ronda", flush=True)
        return

    try:
        print(f"[vigilancia] iniciando comprobación programada ({len(config)} servidor(es) activo(s))", flush=True)

        async with catalogo_lock:
            datos = await obtener_catalogo_api()
            nuevos, desaparecidos = detectar_cambios(datos["catalogo_actual"])

        if not (nuevos or desaparecidos):
            print("[vigilancia] comprobación completada: sin cambios", flush=True)
            return

        embeds = formatear_embeds_cambios(nuevos, desaparecidos)

        enviados = 0
        for guild_id, canal_id in config.items():
            canal = bot.get_channel(canal_id)
            if canal is None:
                # No está en caché (p.ej. justo tras un reinicio); lo pedimos
                # directamente a la API antes de darlo por perdido.
                try:
                    canal = await bot.fetch_channel(canal_id)
                except (discord.NotFound, discord.Forbidden) as e:
                    print(f"[vigilancia] no se pudo acceder al canal {canal_id} (guild {guild_id}): {e}", flush=True)
                    continue
            for embed in embeds:
                await canal.send(embed=embed)
            enviados += 1

        print(f"[vigilancia] cambios detectados: {len(nuevos)} nuevo(s), {len(desaparecidos)} desaparecido(s) — notificado en {enviados}/{len(config)} servidor(es)", flush=True)

    except Exception as e:
        await cerrar_sesion_http()
        print(f"[vigilancia] error comprobando el catálogo: {e}", flush=True)


@vigilar_catalogo.before_loop
async def antes_de_vigilar():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not vigilar_catalogo.is_running():
        vigilar_catalogo.start()
    print(f'Bot conectado como {bot.user}', flush=True)
if __name__ == '__main__':
    keep_alive()
    bot.run(TOKEN)
