import discord
from discord.ext import commands
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import asyncio
import json
import os
from flask import Flask
from threading import Thread
import time

TOKEN = os.getenv('DISCORD_TOKEN')
URL_GAME = 'https://www.game.es/buscar/amiibo'
ARCHIVO_COLECCION = 'mi_coleccion.json'
ARCHIVO_ULTIMO_CATALOGO = 'ultimo_catalogo.json'
UMBRAL_ALERTA = 14.99

intents = discord.Intents.default()
intents.message_content = True
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

def cargar_coleccion():
    if os.path.exists(ARCHIVO_COLECCION):
        with open(ARCHIVO_COLECCION, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def guardar_coleccion(coleccion):
    with open(ARCHIVO_COLECCION, 'w', encoding='utf-8') as f:
        json.dump(coleccion, f, ensure_ascii=False, indent=4)

def cargar_ultimo_catalogo():
    if os.path.exists(ARCHIVO_ULTIMO_CATALOGO):
        with open(ARCHIVO_ULTIMO_CATALOGO, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def guardar_ultimo_catalogo(catalogo):
    with open(ARCHIVO_ULTIMO_CATALOGO, 'w', encoding='utf-8') as f:
        json.dump(catalogo, f, ensure_ascii=False, indent=4)


# --- NAVEGADOR PERSISTENTE ---
# Antes se lanzaba un Chromium nuevo en cada !catalogo (coste de arranque
# innecesario). Ahora se mantiene un único navegador vivo y solo se abre/
# cierra una pestaña (page) por consulta, que es mucho más barato.
_playwright_instance = None
_browser_instance = None
_browser_lock = asyncio.Lock()
_catalogo_lock = asyncio.Lock()  # evita ejecuciones simultáneas de !catalogo

async def obtener_browser():
    global _playwright_instance, _browser_instance
    async with _browser_lock:
        if _browser_instance is None or not _browser_instance.is_connected():
            if _playwright_instance is None:
                _playwright_instance = await async_playwright().start()
            _browser_instance = await _playwright_instance.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
        return _browser_instance

async def cerrar_browser():
    """Fuerza el relanzamiento del navegador en la siguiente petición
    (se usa cuando algo ha ido mal, ej. crash o timeout raro)."""
    global _browser_instance
    async with _browser_lock:
        if _browser_instance is not None:
            try:
                await _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
# ------------------------------


async def hacer_scroll_completo(page, max_intentos=15, espera_ms=2500, intentos_estable=3):
    """Antes: 10 scrolls fijos de 3s cada uno = 30s siempre, cargue lo que
    cargue la página. Ahora: se hace scroll y se comprueba la altura del
    documento; si deja de crecer durante `intentos_estable` rondas seguidas,
    se para. Normalmente termina bastante antes de los 30s originales."""
    altura_anterior = await page.evaluate("document.body.scrollHeight")
    estable = 0
    for _ in range(max_intentos):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(espera_ms)
        altura_actual = await page.evaluate("document.body.scrollHeight")
        if altura_actual == altura_anterior:
            estable += 1
            if estable >= intentos_estable:
                break
        else:
            estable = 0
        altura_anterior = altura_actual


def limpiar_precio(precio_str):
    """Convierte '14,99' -> 14.99. Devuelve None si no se puede."""
    if not precio_str:
        return None
    try:
        return float(precio_str.replace(',', '.'))
    except ValueError:
        return None


def extraer_precio_respaldo(articulo):
    """Cuando data-list-item-price viene a 0, se intenta leer el precio
    visible en el HTML (spans .int / .decimal)."""
    entero = articulo.find('span', class_='int')
    decimal = articulo.find('span', class_='decimal')
    if not entero:
        return None, None

    precio_str = entero.text.strip()
    if decimal:
        dec_texto = decimal.text.strip().replace("'", "").replace(",", "")
        precio_str += "," + dec_texto

    return precio_str, limpiar_precio(precio_str)


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


async def obtener_catalogo_playwright():
    browser = await obtener_browser()
    page = await browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    try:
        await page.goto(URL_GAME)
        await page.wait_for_selector('.figure', state='attached', timeout=15000)
        await page.wait_for_timeout(3000)

        await hacer_scroll_completo(page)

        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        articulos = soup.find_all('div', class_='search-item')

        # Red de seguridad: si el scroll "inteligente" se paró antes de que
        # el catálogo terminara de cargar (lazy-load), seguimos forzando
        # scrolls como en la versión original hasta encontrar algo o
        # agotar los intentos.
        intentos_extra = 0
        while not articulos and intentos_extra < 6:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            articulos = soup.find_all('div', class_='search-item')
            intentos_extra += 1

        print(f"[catalogo] artículos encontrados: {len(articulos)}")
    finally:
        await page.close()

    lista_smash = []
    lista_reacondicionados = []
    coleccion = [c.lower() for c in cargar_coleccion()]

    ultimo_catalogo = cargar_ultimo_catalogo()
    catalogo_actual = {}

    total_encontrados = 0
    tengo_count = 0
    no_tengo_count = 0

    for articulo in articulos:
        caja = articulo.find('a', class_='figure')
        if not caja:
            continue

        nombre = caja.get('data-list-item-name')
        precio_str = caja.get('data-list-item-price')
        link = caja.get('href')

        if not (nombre and precio_str):
            continue

        nombre_lower = nombre.lower()
        if "smash" not in nombre_lower:
            continue

        es_reacondicionado = "reacondicionado" in nombre_lower
        lo_tengo = any(item in nombre_lower for item in coleccion)

        precio_num = limpiar_precio(precio_str)
        if precio_num is None:
            precio_num = 15.00

        # --- ARREGLO PARA PRECIOS A 0€ ---
        if precio_num == 0:
            precio_str_respaldo, precio_num_respaldo = extraer_precio_respaldo(articulo)
            if precio_str_respaldo is not None:
                precio_str = precio_str_respaldo
                precio_num = precio_num_respaldo if precio_num_respaldo is not None else 15.00
        # ---------------------------------

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
        f"📊 Total: {total_encontrados} + (♻️ {len(lista_reacondicionados)}) "
        f"| ✅ {tengo_count} | ❌ {no_tengo_count}\n\n{texto_cambios}\n"
    )

    return resumen, lista_smash, lista_reacondicionados


@bot.command(name='añadir')
async def añadir_coleccion(ctx, *, nombre: str):
    coleccion = cargar_coleccion()
    if nombre not in coleccion:
        coleccion.append(nombre)
        guardar_coleccion(coleccion)
        await ctx.send(f"Añadido a tu colección: **{nombre}**")
    else:
        await ctx.send(f"**{nombre}** ya estaba en tu colección.")

@bot.command(name='quitar')
async def quitar_coleccion(ctx, *, nombre: str):
    coleccion = cargar_coleccion()
    encontrado = None
    for item in coleccion:
        if item.lower() == nombre.lower():
            encontrado = item
            break

    if encontrado:
        coleccion.remove(encontrado)
        guardar_coleccion(coleccion)
        await ctx.send(f"Eliminado de tu colección: **{encontrado}**")
    else:
        await ctx.send(f"No se encontró **{nombre}** en tu colección.")

@bot.command(name='coleccion')
async def mostrar_coleccion(ctx):
    coleccion = cargar_coleccion()

    if not coleccion:
        await ctx.send("Tu colección está vacía actualmente.")
        return

    mensaje = "**Tus Amiibos guardados:**\n"
    for item in coleccion:
        linea = f"• {item}\n"
        if len(mensaje) + len(linea) > 1900:
            await ctx.send(mensaje)
            mensaje = linea
        else:
            mensaje += linea

    if mensaje:
        await ctx.send(mensaje)

@bot.command(name='ayuda')
async def mostrar_ayuda(ctx):
    texto_ayuda = (
        "**Comandos del Bot:**\n"
        "• `!catalogo` - Muestra los Amiibos de Smash en un panel continuo.\n"
        "• `!coleccion` - Muestra la lista completa de Amiibos que tienes guardados.\n"
        "• `!añadir [nombre]` - Añade un Amiibo a tu colección personal.\n"
        "• `!quitar [nombre]` - Elimina un Amiibo de tu colección personal.\n"
        "• `!ayuda` - Muestra este menú de ayuda."
    )
    await ctx.send(texto_ayuda)

@bot.command(name='catalogo')
@commands.cooldown(1, 15, commands.BucketType.guild)
async def mostrar_catalogo(ctx):
    if _catalogo_lock.locked():
        await ctx.send("Ya hay una consulta de catálogo en marcha, espera un momento...")
        return

    mensaje_espera = await ctx.send("Abriendo catalogo...")
    tiempo_inicio = time.time()

    async with _catalogo_lock:
        try:
            resumen, smash_normales, smash_reacondicionados = await obtener_catalogo_playwright()

            tiempo_fin = time.time()
            resultado = round(tiempo_fin - tiempo_inicio, 1)

            if not smash_normales and not smash_reacondicionados:
                await mensaje_espera.edit(content="No hay resultados de Smash en la web.")
                return

            await mensaje_espera.delete()
            await ctx.send(content=f"**Tiempo de respuesta: {resultado}s**")

            # --- PRIMER EMBED: Amiibos normales ---
            descripcion_normal = resumen + "\n".join(smash_normales)
            if len(descripcion_normal) > 4096:
                descripcion_normal = descripcion_normal[:4093] + "..."

            embed_normal = discord.Embed(
                title="🛒 Catálogo de Amiibos (Smash)",
                description=descripcion_normal,
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed_normal)

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
                await ctx.send(embed=embed_reac)

        except asyncio.TimeoutError:
            await mensaje_espera.edit(content="La web de GAME ha tardado demasiado en responder. Inténtalo de nuevo en unos minutos.")
        except Exception as e:
            # Si algo raro pasó (crash del navegador, etc.), forzamos que se
            # relance limpio en la próxima consulta.
            await cerrar_browser()
            await mensaje_espera.edit(content=f"Ha ocurrido un error al cargar la web: {e}")

@mostrar_catalogo.error
async def mostrar_catalogo_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Espera {error.retry_after:.0f}s antes de volver a pedir el catálogo.")
    else:
        raise error

@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user}')
    # Precargamos el navegador para que el primer !catalogo no pague
    # el coste de arranque de Chromium.
    try:
        await obtener_browser()
        print('Navegador listo.')
    except Exception as e:
        print(f'No se pudo precargar el navegador: {e}')

if __name__ == '__main__':
    keep_alive()
    bot.run(TOKEN)
