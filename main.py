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

# --- VIGILANCIA PERIÓDICA DEL CATÁLOGO ---
INTERVALO_MINUTOS = 20  # cada cuánto se comprueba si ha cambiado el catálogo
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
    return cargar_json(ARCHIVO_COLECCION, [])

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
    timeout = aiohttp.ClientTimeout(total=15)  # evita quedarse colgado si la API no responde

    async with aiohttp.ClientSession(timeout=timeout) as session:
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
    lista_smash = []
    lista_reacondicionados = []
    coleccion = [c.lower() for c in cargar_coleccion()]
    
    ultimo_catalogo = cargar_ultimo_catalogo()
    catalogo_actual = {}
    
    total_encontrados = 0
    tengo_count = 0
    no_tengo_count = 0
    
    for producto in todos_los_productos:
        nombre = producto.get('Name')
        if not nombre:
            continue
            
        nombre_lower = nombre.lower()
        if "smash" not in nombre_lower:
            continue
            
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

        es_reacondicionado = "reacondicionado" in nombre_lower
        lo_tengo = any(item in nombre_lower for item in coleccion)
            
        if es_reacondicionado:
            texto = formatear_item(nombre, precio_str, lo_tengo, link=link, mostrar_alerta=False)
            lista_reacondicionados.append(texto)
        else:
            catalogo_actual[nombre] = precio_str
            total_encontrados += 1
            
            if lo_tengo:
                tengo_count += 1
            else:
                no_tengo_count += 1
                
            texto = formatear_item(
                nombre, precio_str, lo_tengo,
                link=link, precio_num=precio_num, mostrar_alerta=True
            )
            lista_smash.append(texto)
            
    nuevos = []
    desaparecidos = []
    
    for nombre, precio in catalogo_actual.items():
        if nombre not in ultimo_catalogo:
            nuevos.append(f"{nombre} — {precio}€")
            
    for nombre, precio in ultimo_catalogo.items():
        if nombre not in catalogo_actual:
            desaparecidos.append(f"{nombre} — {precio}€")
            
    texto_cambios = ""
    if not ultimo_catalogo:
        texto_cambios = "No hay nuevos cambios\n"
    elif nuevos or desaparecidos:
        if nuevos:
            texto_cambios += "**Nuevo:**\n"
            for item in nuevos:
                texto_cambios += f"     {item}\n"
        if desaparecidos:
            texto_cambios += "**Desaparece:**\n"
            for item in desaparecidos:
                texto_cambios += f"     {item}\n"
    else:
        texto_cambios = "No hay nuevos cambios\n"
        
    guardar_ultimo_catalogo(catalogo_actual)
    
    resumen = (
        f"📊 Total: {total_encontrados} (♻️ {len(lista_reacondicionados)}) "
        f"| ✅ {tengo_count} | ❌ {no_tengo_count}\n\n{texto_cambios}\n"
    )
    
    return resumen, lista_smash, lista_reacondicionados, nuevos, desaparecidos


@bot.tree.command(name='añadir',description='Añade un amiibo a la colección')
async def añadir_coleccion(interaction: discord.Interaction, *, nombre: str):
    coleccion = cargar_coleccion()
    if nombre not in coleccion:
        coleccion.append(nombre)
        guardar_coleccion(coleccion)
        await interaction.response.send_message(f"Añadido a tu colección: **{nombre}**")
    else:
        await interaction.response.send_message(f"**{nombre}** ya estaba en tu colección.")

@bot.tree.command(name='quitar',description='Quita un amiibo de la colección')
async def quitar_coleccion(interaction: discord.Interaction, *, nombre: str):
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
        "• `/catalogo` - Muestra los Amiibos de Smash en un panel continuo.\n"
        "• `/vigilancia` - Activa/desactiva el aviso automático cuando cambie el catálogo.\n"
        "• `/coleccion` - Muestra la lista completa de Amiibos que tienes guardados.\n"
        "• `/añadir [nombre]` - Añade un Amiibo a tu colección personal.\n"
        "• `/quitar [nombre]` - Elimina un Amiibo de tu colección personal.\n"
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
            resumen, smash_normales, smash_reacondicionados, _, _ = await obtener_catalogo_api()

        tiempo_fin = time.time()
        resultado = round(tiempo_fin - tiempo_inicio, 1)

        if not smash_normales and not smash_reacondicionados:
            await interaction.edit_original_response(content="No hay resultados de Smash en la web.")
            return

        # 2. Modificamos el mensaje inicial de carga con el tiempo de respuesta
        await interaction.edit_original_response(content=f"**Tiempo de respuesta: {resultado}s**")

        # --- PRIMER EMBED: Amiibos normales ---
        descripcion_normal = resumen + "\n".join(smash_normales)
        if len(descripcion_normal) > 4096:
            descripcion_normal = descripcion_normal[:4093] + "..."

        embed_normal = discord.Embed(
            title="🛒 Catálogo de Amiibos (Smash)",
            description=descripcion_normal,
            color=discord.Color.blue()
        )
        
        # Para enviar los embeds extra usamos followup porque ya respondimos antes
        await interaction.followup.send(embed=embed_normal)

        # --- SEGUNDO EMBED: Amiibos reacondicionados ---
        if smash_reacondicionados:
            descripcion_reac = "\n".join(smash_reacondicionados)
            if len(descripcion_reac) > 4096:
                descripcion_reac = descripcion_reac[:4093] + "..."

            embed_reac = discord.Embed(
                title="♻️ Reacondicionados",
                description=descripcion_reac,
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed_reac)

    except Exception as e:
        await interaction.edit_original_response(content=f"Ha ocurrido un error al cargar la web: {e}")

@mostrar_catalogo.error
async def mostrar_catalogo_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"Espera {error.retry_after:.0f}s antes de volver a pedir el catálogo.", ephemeral=True)
    else:
        raise error


def formatear_embed_cambios(nuevos, desaparecidos):
    ahora = datetime.now().strftime('%d/%m/%Y %H:%M')

    descripcion = ""
    if nuevos:
        descripcion += "**:arrow_forward: Añadido:**\n" + "\n".join(nuevos) + "\n\n"
    if desaparecidos:
        descripcion += "**:arrow_backward: Removido:**\n" + "\n".join(desaparecidos)

    return discord.Embed(
        title=f"**__Cambios {ahora}__**",
        description=descripcion.strip()[:4096],
        color=discord.Color.gold()
    )


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
        async with catalogo_lock:
            _, _, _, nuevos, desaparecidos = await obtener_catalogo_api()

        if not (nuevos or desaparecidos):
            return  # sin cambios, no se envía nada

        embed = formatear_embed_cambios(nuevos, desaparecidos)

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
            await canal.send(embed=embed)

    except Exception as e:
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
