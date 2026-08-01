import discord
from discord.ext import commands
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import json
import os
from flask import Flask
from threading import Thread

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
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        await page.goto(URL_GAME)
        await page.wait_for_selector('.figure', timeout=15000)
        
        # --- LÓGICA DE SCROLL MEJORADA ---
        intentos_vacios = 0
        for _ in range(15): # Aumentamos el máximo de veces que puede bajar
            altura_anterior = await page.evaluate("document.body.scrollHeight")
            
            # Bajamos hasta abajo
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            
            # Esperamos un poco más (2.5 segundos)
            await page.wait_for_timeout(2500)
            
            altura_nueva = await page.evaluate("document.body.scrollHeight")
            
            # Si la altura es igual, suma un fallo
            if altura_nueva == altura_anterior:
                intentos_vacios += 1
                # Solo se rinde si ha fallado 2 veces seguidas
                if intentos_vacios >= 2:
                    break
            else:
                # Si ha cargado algo nuevo, resetea los fallos a 0
                intentos_vacios = 0
        # ---------------------------------
        
        html = await page.content()
        await browser.close()
        
    soup = BeautifulSoup(html, 'html.parser')
    cajas = soup.find_all('a', class_='figure') 
    
    lista_smash = []
    coleccion = [c.lower() for c in cargar_coleccion()]
    
    ultimo_catalogo = cargar_ultimo_catalogo()
    catalogo_actual = {} 
    
    total_encontrados = 0
    tengo_count = 0
    no_tengo_count = 0
    
    for caja in cajas:
        nombre = caja.get('data-list-item-name')
        precio_str = caja.get('data-list-item-price')
        link = caja.get('href')
        
        if nombre and precio_str:
            nombre_lower = nombre.lower()
            
            if "reacondicionado" in nombre_lower:
                continue
            
            if "smash" in nombre_lower:
                catalogo_actual[nombre] = precio_str
                total_encontrados += 1
                lo_tengo = any(item in nombre_lower for item in coleccion)
                
                try:
                    precio_limpio = precio_str.replace(',', '.')
                    precio_num = float(precio_limpio)
                except ValueError:
                    precio_num = 15.00
                
                if lo_tengo:
                    tengo_count += 1
                    texto = f"**~~{nombre}~~** — {precio_str}€"
                else:
                    no_tengo_count += 1
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
    
    resumen = f"📊 Total: {total_encontrados} | ✅ {tengo_count} | ❌ {no_tengo_count}\n\n{texto_cambios}\n"
    return resumen, lista_smash

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
    
    try:
        resumen, smash = await obtener_catalogo_playwright()
        
        if not smash:
            await mensaje_espera.edit(content="No hay resultados de Smash en la web.")
            return

        descripcion_total = resumen + "\n".join(smash)

        # Discord tiene un límite de 4096 caracteres para la descripción del embed
        if len(descripcion_total) > 4096:
            descripcion_total = descripcion_total[:4093] + "..."

        embed = discord.Embed(
            title="🛒 Catálogo de Amiibos (Smash)",
            description=descripcion_total,
            color=discord.Color.blue()
        )
            
        await mensaje_espera.delete()
        await ctx.send(embed=embed)
            
    except Exception as e:
        await mensaje_espera.edit(content=f"Ha ocurrido un error al cargar la web: {e}")

@bot.event
async def on_ready():
    print(f'Bot conectado como {bot.user}')

if __name__ == '__main__':
    keep_alive()
    bot.run(TOKEN)