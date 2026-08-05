import discord
from discord.ext import commands
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import json
import os
from flask import Flask
from threading import Thread
import time

TOKEN = os.getenv('DISCORD_TOKEN')
URL_GAME = 'https://www.game.es/buscar/amiibo' 
ARCHIVO_COLECCION = 'mi_coleccion.json'
ARCHIVO_ULTIMO_CATALOGO = 'ultimo_catalogo.json'

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

async def obtener_catalogo_playwright():
    async with async_playwright() as p:
        # Añadido los comandos para usar menos memoria en Render
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )
        page = await browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        await page.goto(URL_GAME)
        
        await page.wait_for_selector('.figure', state='attached', timeout=15000)
        await page.wait_for_timeout(3000)
        
        for _ in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)
        
        html = await page.content()
        await browser.close()
        
    soup = BeautifulSoup(html, 'html.parser')
    # Buscamos la "caja grande" de cada producto en lugar de solo el enlace
    articulos = soup.find_all('div', class_='search-item') 
    
    lista_smash = []
    lista_reacondicionados = []
    coleccion = [c.lower() for c in cargar_coleccion()]
    
    ultimo_catalogo = cargar_ultimo_catalogo()
    catalogo_actual = {} 
    
    total_encontrados = 0
    tengo_count = 0
    no_tengo_count = 0
    
    for articulo in articulos:
        # Extraemos la "caja pequeña" donde están los datos ocultos
        caja = articulo.find('a', class_='figure')
        if not caja:
            continue
            
        nombre = caja.get('data-list-item-name')
        precio_str = caja.get('data-list-item-price')
        link = caja.get('href')
        
        if nombre and precio_str:
            nombre_lower = nombre.lower()
            
            if "smash" in nombre_lower:
                es_reacondicionado = "reacondicionado" in nombre_lower
                lo_tengo = any(item in nombre_lower for item in coleccion)
                
                try:
                    precio_limpio = precio_str.replace(',', '.')
                    precio_num = float(precio_limpio)
                except ValueError:
                    precio_num = 15.00

                # --- ARREGLO PARA PRECIOS A 0€ ---
                if precio_num == 0:
                    # Buscamos el precio en toda la caja grande
                    entero = articulo.find('span', class_='int')
                    decimal = articulo.find('span', class_='decimal')
                    
                    if entero:
                        precio_str = entero.text.strip()
                        if decimal:
                            # Limpiamos la comilla del decimal que muestra la web
                            dec_texto = decimal.text.strip().replace("'", "").replace(",", "")
                            precio_str += "," + dec_texto
                        
                        try:
                            precio_num = float(precio_str.replace(',', '.'))
                        except ValueError:
                            precio_num = 15.00
                # ---------------------------------
                
                if es_reacondicionado:
                    # Formato para reacondicionados igual que el normal (con precio, sin alerta)
                    if lo_tengo:
                        texto = f"**~~{nombre}~~** — {precio_str}€"
                    else:
                        if link:
                            if link.startswith('/'):
                                link_completo = f"https://www.game.es{link}"
                            else:
                                link_completo = link
                            texto = f"{nombre} — {precio_str}€ ([Ver web]({link_completo}))"
                        else:
                            texto = f"{nombre} — {precio_str}€"
                            
                    lista_reacondicionados.append(texto)
                else:
                    # Formato para Amiibos nuevos (se mantiene igual)
                    catalogo_actual[nombre] = precio_str
                    total_encontrados += 1
                    
                    if lo_tengo:
                        tengo_count += 1
                        texto = f"**~~{nombre}~~** — {precio_str}€"
                    else:
                        no_tengo_count += 1
                        # Aquí la alarma se queda funcionando para los nuevos
                        alerta = " 🚨" if precio_num < 14.99 else ""
                        
                        if link:
                            if link.startswith('/'):
                                link_completo = f"https://www.game.es{link}"
                            else:
                                link_completo = link
                            texto = f"{nombre} — {precio_str}€{alerta} ([Ver web]({link_completo}))"
                        else:
                            texto = f"{nombre} — {precio_str}€{alerta}"
                            
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
    
    resumen = f"📊 Total: {total_encontrados} + (♻️ {len(lista_reacondicionados)}) | ✅ {tengo_count} | ❌ {no_tengo_count}\n\n{texto_cambios}\n"
    
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
async def mostrar_catalogo(ctx):
    mensaje_espera = await ctx.send("Abriendo catalogo...")
    
    tiempo_inicio = time.time()
    
    try:
        # Ahora recibimos las dos listas por separado
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
        
        # Cortamos a las 4096 letras solo como medida de seguridad extrema
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
            
    except Exception as e:
        await mensaje_espera.edit(content=f"Ha ocurrido un error al cargar la web: {e}")

@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user}')

if __name__ == '__main__':
    keep_alive()
    bot.run(TOKEN)
