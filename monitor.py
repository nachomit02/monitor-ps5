"""Monitor de stock, precio y cuotas de la PlayStation 5 (Standard, Digital y Pro) en tiendas argentinas.

Uso:
  python monitor.py                 revisa todo y avisa por Telegram si algo cambió
  python monitor.py --dry-run       revisa y muestra resultados, sin mandar mensajes ni guardar estado
  python monitor.py --resumen       revisa, avisa y además manda el estado de todas las tiendas
  python monitor.py --test          manda un mensaje de prueba a Telegram
"""
import csv
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
SECRETS = BASE / "secrets.json"
STATE = BASE / "state.json"
HISTORIAL = BASE / "historial.csv"
SALUD = BASE / "salud.json"
LOG = BASE / "monitor.log"
ART = timezone(timedelta(hours=-3))  # Argentina no tiene horario de verano; la nube corre en UTC
FALLAS_PARA_AVISAR = 3  # corridas seguidas con error antes de avisar que una tienda se rompió
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


def ahora():
    return datetime.now(ART).replace(tzinfo=None)


def log(msg):
    linea = f"{ahora():%Y-%m-%d %H:%M:%S} {msg}"
    print(linea.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(linea + "\n")


def leer_json(path, defecto):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return defecto


def guardar_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tienda_de(url):
    host = urllib.parse.urlparse(url).netloc.replace("www.", "")
    return host.split(".")[0].capitalize()


def fmt(precio):
    return f"${precio:,.0f}".replace(",", ".") if precio else "precio no detectado"


def http_json(url, datos=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=datos, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- Qué modelo es cada publicación

def sin_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


ES_PS5 = re.compile(r"play\s?-?station\s?5|\bps\s?5\b|playstation5")
# accesorios, juegos, otras consolas y equipos usados: no son la consola que buscamos
EXCLUIR = re.compile(
    r"reacondicionad|refurb|usad[oa]|seminuev|\bopen box\b|portal|\bvr\s?2?\b|gafas|lentes|cover|cubierta|carcasa|"
    r"funda|skin|vinilo|lectora|unidad de disco|disk drive|\bp/\s?consola|para consola|compatible|soporte|"
    r"\bbase\b|cargador|estacion de carga|auricular|headset|volante|camara|\bcable\b|joystick ps5 sony|"
    r"playstation 4|\bps4\b|playstation 3|playstation 2|\bretro\b|repuesto|ventilador|cooler|mochila|bolso|"
    r"\bcaja\b vacia|tarjeta|gift|suscripcion|plus\b \d+ meses|membresia|remote|sticker|protector|controlador|"
    r"joystck|joysticks? inalambric|dualsense (edge|first|blanco|negro)|media remote|\bmanual\b")
# además tiene que parecer una consola: la palabra "consola" o alguna característica de hardware
SEÑAL_CONSOLA = re.compile(
    r"consola|\b\d{3,4}\s?gb\b|\b\d\s?tb\b|\bslim\b|\bhw\b|cfi|edition|edicion|standard|estandar|standar\b|"
    r"digital|\bpro\b|bundle|fisica|\bdisco\b")
# nombres que empiezan así son accesorios o juegos aunque mencionen la consola
EMPIEZA_ACCESORIO = re.compile(
    r"^(joystick|control|mando|dualsense|juego|videojuego|video juego|disco|ssd|memoria|teclado|mouse|"
    r"auricular|cargador|soporte|funda|cover|cubierta|base|cable|volante|camara|reproductor|gafas)\b")


IMPORTADA = re.compile(r"internacional|importad|\bperu\b|\busa\b|japon|europea|\bversion (us|eu)\b|nocnoc")


def clasificar(nombre):
    """'standard', 'digital', 'pro' o None si no es una PS5 nueva."""
    n = sin_acentos(nombre)
    if not ES_PS5.search(n) or EXCLUIR.search(n) or EMPIEZA_ACCESORIO.search(n.strip()) \
            or not SEÑAL_CONSOLA.search(n):
        return None
    if re.search(r"\bpro\b|cfi-?7\d{3}", n):
        return "pro"
    if re.search(r"\bdigital\b|\bdig\b|edicion digital|cfi-?\d{4}b|\bdigital edition\b", n):
        return "digital"
    return "standard"


# ---------------------------------------------------------------- Cuotas

def cuotas_vtex(installments):
    """La mejor opción de cuotas: la mayor cantidad sin interés, o si no hay, la mayor con interés."""
    planes = [i for i in installments if i.get("NumberOfInstallments", 1) > 1 and i.get("Value")]
    if not planes:
        return None
    sin = [i for i in planes if not i.get("InterestRate")]
    elegidos = sin or planes
    n = max(i["NumberOfInstallments"] for i in elegidos)
    de_n = [i for i in elegidos if i["NumberOfInstallments"] == n]
    mejor = min(de_n, key=lambda i: i["Value"])
    tarjetas = sorted({i.get("PaymentSystemName", "") for i in de_n if i["Value"] == mejor["Value"]} - {""})
    return {"n": n, "valor": round(mejor["Value"]), "sin_interes": bool(sin),
            "total": round(mejor.get("TotalValuePlusInterestRate") or mejor["Value"] * n), "tarjetas": tarjetas}


CUOTAS_RE = re.compile(
    r"(\d{1,2})\s*cuotas?\s*(sin inter[eé]s|s/\s?inter[eé]s|sin recargo|fijas)?\s*(?:de\s*)?\$\s?(\d{1,3}(?:\.\d{3})+)",
    re.I)
CUOTAS_SIN_VALOR_RE = re.compile(r"(\d{1,2})\s*cuotas?\s*(sin inter[eé]s|s/\s?inter[eé]s|sin recargo)", re.I)


def cuotas_texto(texto, precio):
    """Lee 'Hasta 12 cuotas sin interés de $150.000' en el texto de una página."""
    opciones = []
    for m in CUOTAS_RE.finditer(texto):
        n, sin, valor = int(m.group(1)), bool(m.group(2)) and "fija" not in m.group(2).lower(), int(m.group(3).replace(".", ""))
        if 2 <= n <= 36:
            opciones.append({"n": n, "valor": valor, "sin_interes": sin, "total": valor * n, "tarjetas": []})
    if not opciones and precio:
        for m in CUOTAS_SIN_VALOR_RE.finditer(texto):
            n = int(m.group(1))
            if 2 <= n <= 36:
                opciones.append({"n": n, "valor": round(precio / n), "sin_interes": True, "total": precio, "tarjetas": []})
    if not opciones:
        return None
    sin = [o for o in opciones if o["sin_interes"]]
    return max(sin or opciones, key=lambda o: o["n"])


def texto_cuotas(c):
    if not c:
        return ""
    tarjetas = f" ({', '.join(c['tarjetas'][:3])}{'…' if len(c['tarjetas']) > 3 else ''})" if c.get("tarjetas") else ""
    if c["sin_interes"]:
        return f"{c['n']} cuotas sin interés de {fmt(c['valor'])}{tarjetas}"
    return f"{c['n']} cuotas de {fmt(c['valor'])}, total {fmt(c['total'])}{tarjetas}"


# ---------------------------------------------------------------- Telegram

def telegram_api(token, metodo, params=None):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.loads(r.read().decode())


def enviar_telegram(texto):
    sec = leer_json(SECRETS, {})
    if not sec.get("token") or not sec.get("chat_id"):
        log("Telegram no configurado")
        return
    telegram_api(sec["token"], "sendMessage", {
        "chat_id": sec["chat_id"], "text": texto, "parse_mode": "HTML",
        "disable_web_page_preview": "true"})


# ---------------------------------------------------------------- Tiendas VTEX (catálogo público en JSON)

CONSULTAS_VTEX = ["consola playstation 5", "playstation 5 slim", "playstation 5 pro", "ps5 consola"]


def vtex_entregable(tienda, sku, vendedor, codigo_postal):
    """El catálogo puede decir "en stock" aunque el checkout no lo venda: simula la compra
    y exige al menos un envío o retiro."""
    cuerpo = json.dumps({"items": [{"id": sku, "quantity": 1, "seller": vendedor}],
                         "postalCode": codigo_postal, "country": "ARG"}).encode()
    sim = http_json(f"{tienda}/api/checkout/pub/orderForms/simulation", cuerpo,
                    {"Content-Type": "application/json"})
    return any(it.get("availability") == "available" for it in sim.get("items", [])) and \
        any(info.get("slas") for info in sim.get("logisticsInfo", []))


def precio_vtex(oferta):
    """Algunos vendedores cargan el precio sin IVA; el pago en 1 cuota trae el precio final."""
    una = [i["TotalValuePlusInterestRate"] for i in oferta.get("Installments", [])
           if i.get("NumberOfInstallments") == 1 and i.get("TotalValuePlusInterestRate")]
    return int(round(max(min(una), oferta["Price"]))) if una else int(round(oferta["Price"]))


def buscar_vtex(tienda, codigo_postal):
    tienda = tienda.rstrip("/")
    productos = {}
    for q in CONSULTAS_VTEX:
        url = f"{tienda}/api/catalog_system/pub/products/search?ft={urllib.parse.quote(q)}&_from=0&_to=49"
        for prod in http_json(url):
            productos[prod["productId"]] = prod
    resultados = []
    for prod in productos.values():
        modelo = clasificar(prod.get("productName", ""))
        if not modelo:
            continue
        ofertas = []
        for it in prod.get("items", []):
            for s in it.get("sellers", []):
                o = s.get("commertialOffer", {})
                if o.get("AvailableQuantity", 0) > 0 and o.get("Price"):
                    ofertas.append((precio_vtex(o), it["itemId"], s["sellerId"], s.get("sellerName", ""), o))
        ofertas.sort(key=lambda x: x[0])
        elegida = None
        for oferta in ofertas[:3]:  # la más barata que de verdad se pueda comprar
            try:
                if vtex_entregable(tienda, oferta[1], oferta[2], codigo_postal):
                    elegida = oferta
                    break
            except Exception:
                continue
        res = {"url": prod["link"], "titulo": prod["productName"][:120], "modelo": modelo,
               "estado": "disponible" if elegida else "sin_stock", "precio": None, "cuotas": None}
        if elegida:
            res.update(precio=elegida[0], cuotas=cuotas_vtex(elegida[4].get("Installments", [])),
                       vendedor=elegida[3])
        resultados.append(res)
    return resultados


# ---------------------------------------------------------------- Mercado Libre (API oficial, permiso de app)

ML_API = "https://api.mercadolibre.com"
_ml = {}


def ml_get(ruta):
    """Usa un permiso de la app (client_credentials): no toca el permiso del monitor de teles."""
    if "token" not in _ml:
        sec = leer_json(SECRETS, {})
        datos = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": sec["ml_client_id"],
                                        "client_secret": sec["ml_secret"]}).encode()
        _ml["token"] = http_json(f"{ML_API}/oauth/token", datos)["access_token"]
    return http_json(f"{ML_API}{ruta}", headers={"Authorization": f"Bearer {_ml['token']}"})


def buscar_mercadolibre():
    candidatos = {}
    for q in ("playstation 5", "ps5 pro", "playstation 5 slim digital"):
        for offset in (0, 50, 100):
            try:
                res = ml_get(f"/products/search?status=active&site_id=MLA&domain_id=MLA-GAME_CONSOLES"
                             f"&limit=50&offset={offset}&q={urllib.parse.quote(q)}")
            except urllib.error.HTTPError as e:
                log(f"Mercado Libre búsqueda '{q}' offset {offset}: HTTP {e.code}")
                break
            for r in res.get("results", []):
                modelo = clasificar(r.get("name", ""))
                if modelo:
                    candidatos[r["id"]] = (modelo, r["name"])
            if len(res.get("results", [])) < 50:
                break
    resultados = []
    for pid, (modelo, nombre) in list(candidatos.items())[:90]:
        try:
            ofertas = ml_get(f"/products/{pid}/items").get("results", [])
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = sin publicaciones activas
                log(f"Mercado Libre {pid}: HTTP {e.code}")
                continue
            ofertas = []
        precios = [int(o["price"]) for o in ofertas if o.get("price") and o.get("condition", "new") == "new"
                   and o.get("currency_id", "ARS") == "ARS"]
        resultados.append({"url": f"https://www.mercadolibre.com.ar/p/{pid}", "titulo": nombre[:120],
                           "modelo": modelo, "estado": "disponible" if precios else "sin_stock",
                           "precio": min(precios) if precios else None, "cuotas": None})
    return resultados


# ---------------------------------------------------------------- Tiendas sin API (navegador)

SIN_STOCK = ["sin stock", "agotado", "no disponible", "no está disponible", "no se encuentra disponible",
             "no hay stock", "publicación pausada", "te avisaremos cuando", "avisame cuando", "avisarme cuando"]
CON_STOCK = ["agregar al carrito", "añadir al carrito", "comprar ahora", "agregar a la bolsa", "comprar"]
JS_BLOQUE = r"""([sin, con]) => {
  const h1 = [...document.querySelectorAll('h1')].find(e => e.offsetParent !== null && e.innerText.trim());
  if (!h1) return {titulo: '', bloque: '', meta: null};
  const meta = document.querySelector('meta[property="product:price:amount"], [itemprop="price"]');
  let el = h1, bloque = '';
  for (let i = 0; i < 15 && el.parentElement; i++) {
    el = el.parentElement;
    const t = el.innerText.replace(/\s+/g, ' ');
    if (t.length > 6000) break;
    const tl = t.toLowerCase();
    const hayPrecio = /\$\s?\d/.test(t);
    if (sin.some(k => tl.includes(k)) || (hayPrecio && con.some(k => tl.includes(k)))) { bloque = t; break; }
  }
  return {titulo: h1.innerText.replace(/\s+/g, ' ').trim(), bloque,
          meta: meta ? (meta.getAttribute('content') || meta.innerText) : null};
}"""
PRECIO_RE = re.compile(r"\$\s?(\d{1,3}(?:\.\d{3})+)(?:,\d{2})?")
NO_PRECIO_RE = re.compile(
    r"(sin impuestos[^$]{0,20}|s/imp[^$]{0,20}|cuotas?[^$]{0,30}de\s|\bde\s|meses\s-\s|ahorr[áa]s?\s)\$\s?[\d.]+(?:,\d{2})?",
    re.I)


def extraer_precio(texto, piso):
    limpio = NO_PRECIO_RE.sub(" ", texto)
    precios = [int(m.group(1).replace(".", "")) for m in PRECIO_RE.finditer(limpio)]
    precios = [p for p in precios if p >= piso]
    return min(precios) if precios else None


def bloqueada(page):
    return ("account-verification" in page.url or "/login" in page.url
            or "request could not be satisfied" in page.title().lower()
            or "access denied" in page.title().lower() or "just a moment" in page.title().lower())


def links_de_busqueda(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    if bloqueada(page):
        return None
    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href, (e.innerText || e.title || e.getAttribute('aria-label') || '')])")
    encontrados = {}
    host = urllib.parse.urlparse(page.url).netloc
    aqui = page.url.split("?")[0].split("#")[0]
    for href, texto in links:
        limpio = href.split("?")[0].split("#")[0]
        if urllib.parse.urlparse(href).netloc != host or limpio == aqui or "search" in href or "busca" in href \
                or "?q=" in href or "?s=" in href:
            continue
        texto = texto.strip()
        modelo = clasificar(texto) if texto else clasificar(urllib.parse.unquote(limpio).replace("-", " "))
        if modelo:
            encontrados.setdefault(href.split("?")[0].split("#")[0], modelo)
    if not encontrados:
        log(f"  buscador sin coincidencias: {len(links)} links, título '{page.title()[:80]}'")
    return list(encontrados.items())[:8]


def revisar_pagina(page, url, pisos):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    if bloqueada(page):
        return {"estado": "bloqueado", "precio": None}
    r = page.evaluate(JS_BLOQUE, [SIN_STOCK, CON_STOCK])
    titulo, bloque = r["titulo"], r["bloque"].lower()
    modelo = clasificar(titulo)
    if not modelo:
        return {"estado": "no_existe", "precio": None, "titulo": titulo[:120]}
    if any(k in bloque for k in SIN_STOCK):
        estado = "sin_stock"
    elif any(k in bloque for k in CON_STOCK):
        estado = "disponible"
    else:
        estado = "desconocido"
    precio = cuotas = None
    if estado == "disponible":
        meta = re.sub(r"[^\d.]", "", r["meta"] or "")
        precio = int(float(meta)) if meta and float(meta) >= pisos[modelo] else extraer_precio(r["bloque"], pisos[modelo])
        cuotas = cuotas_texto(r["bloque"], precio)
    return {"estado": estado, "precio": precio, "cuotas": cuotas, "titulo": titulo[:120], "modelo": modelo}


# ---------------------------------------------------------------- Historial de precios

def leer_historial():
    if not HISTORIAL.exists():
        return []
    with HISTORIAL.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def anotar_historial(filas):
    nuevo = not HISTORIAL.exists()
    with HISTORIAL.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha", "producto", "tienda", "precio", "url"])
        w.writerows(filas)


def dia(fecha):
    return f"{fecha[8:10]}/{fecha[5:7]}"


def contexto_precio(historial, producto, tienda, precio):
    """Compara con los últimos 30 días: detecta 'ofertas' que en realidad subieron antes de bajar."""
    if precio is None:
        return ""
    desde = f"{ahora() - timedelta(days=30):%Y-%m-%d %H:%M}"
    hace_una_semana = f"{ahora() - timedelta(days=7):%Y-%m-%d %H:%M}"
    previos = [h for h in historial if h["producto"] == producto and h["fecha"] >= desde]
    if not previos:
        return ""
    lineas = []
    minimo = min(previos, key=lambda h: int(h["precio"]))
    if precio <= int(minimo["precio"]):
        lineas.append(f"🏆 el más barato que registré desde el {dia(previos[0]['fecha'])}")
    else:
        lineas.append(f"📊 no es el mínimo: el {dia(minimo['fecha'])} estaba a {fmt(int(minimo['precio']))} "
                      f"en {minimo['tienda']}")
    misma_tienda = [h for h in previos if h["tienda"] == tienda and h["fecha"] < hace_una_semana]
    if misma_tienda:
        antes = min(misma_tienda, key=lambda h: int(h["precio"]))
        if precio > int(antes["precio"]):
            if antes["tienda"] == minimo["tienda"] and antes["precio"] == minimo["precio"]:
                lineas = []
            lineas.append(f"⚠️ ojo: en {tienda} estaba a {fmt(int(antes['precio']))} el {dia(antes['fecha'])}, "
                          "la 'oferta' es más cara que eso")
    return "\n" + "\n".join(lineas)


# ---------------------------------------------------------------- Salud del monitor

def actualizar_salud(salud_corrida):
    salud = leer_json(SALUD, {})
    avisos = []
    for tienda, motivo in salud_corrida.items():
        s = salud.setdefault(tienda, {"fallas": 0, "avisado": False})
        if motivo is None:
            if s["avisado"]:
                avisos.append(f"✅ {tienda} volvió a andar (monitor PS5).")
            s.update(fallas=0, avisado=False, motivo=None)
            continue
        s["fallas"] += 1
        s["motivo"] = motivo[:200]
        if s["fallas"] >= FALLAS_PARA_AVISAR and not s["avisado"]:
            s["avisado"] = True
            avisos.append(f"🔧 <b>{tienda}</b> falló {s['fallas']} veces seguidas (monitor PS5).\n"
                          f"Motivo: {html.escape(motivo[:200])}")
    return salud, avisos


ICONOS = {"disponible": "🟢", "sin_stock": "🔴", "no_publicado": "⚪", "no_existe": "⚫", "bloqueado": "🚫",
          "desconocido": "❔"}


def resumen(estado):
    lineas = [f"📋 <b>Resumen PS5 {ahora():%d/%m %H:%M}</b>"]
    por_producto = {}
    for url, v in estado.items():
        por_producto.setdefault(v.get("producto", "?"), {})
        t = tienda_de(url)
        previo = por_producto[v.get("producto", "?")].get(t)
        if not previo or (v.get("precio") and (not previo.get("precio") or v["precio"] < previo["precio"])):
            por_producto[v.get("producto", "?")][t] = v
    for prod, tiendas in por_producto.items():
        lineas.append(f"\n<b>{prod}</b>")
        for t, v in sorted(tiendas.items(), key=lambda x: x[1].get("precio") or 10 ** 12):
            precio = f" {fmt(v['precio'])}" if v.get("precio") else ""
            lineas.append(f"{ICONOS.get(v['estado'], '❔')} {t}: {v['estado'].replace('_', ' ')}{precio}")
    return "\n".join(lineas)


# ---------------------------------------------------------------- Principal

def main(dry_run=False, con_resumen=False):
    config = leer_json(CONFIG, {})
    productos = {p["modelo"]: p for p in config["productos"]}
    pisos = {m: p["precio_max"] * 0.4 for m, p in productos.items()}  # menos que eso es un accesorio
    estado_previo = leer_json(STATE, {})
    # lo de tiendas que fallan en esta corrida se conserva hasta 2 días, para no repetir avisos
    limite = f"{ahora() - timedelta(days=2):%Y-%m-%d %H:%M}"
    estado_nuevo = {u: v for u, v in estado_previo.items() if v.get("visto", "") >= limite}
    historial = leer_historial()
    filas_historial, avisos = [], []
    visto = f"{ahora():%Y-%m-%d %H:%M}"
    salud_corrida = {}
    conocidas = set(config.get("tiendas_bloqueadas_conocidas", []))
    cp = config.get("codigo_postal", "1425")

    def anotar(tienda, motivo=None):
        if motivo is None:
            salud_corrida[tienda] = None
        else:
            salud_corrida.setdefault(tienda, motivo)

    resultados = []  # (tienda, dict)

    def marcar_no_publicado(clave, tienda, encontrados):
        for modelo, prod in productos.items():
            if not any(r["modelo"] == modelo for r in encontrados):
                estado_nuevo[f"{clave}#{modelo}"] = {"estado": "no_publicado", "precio": None,
                                                     "producto": prod["nombre"], "visto": visto}

    for tienda in config.get("vtex", []):
        nombre = tienda_de(tienda)
        try:
            encontrados = buscar_vtex(tienda, cp)
        except Exception as e:
            log(f"ERROR catálogo {tienda}: {str(e)[:150]}")
            if nombre not in conocidas:
                anotar(nombre, f"error en el catálogo: {str(e)[:150]}")
            continue
        anotar(nombre)
        resultados += [(nombre, r) for r in encontrados]
        marcar_no_publicado(tienda, nombre, encontrados)

    if config.get("mercadolibre"):
        try:
            encontrados = buscar_mercadolibre()
            anotar("Mercadolibre")
            resultados += [("Mercadolibre", r) for r in encontrados]
            marcar_no_publicado("https://www.mercadolibre.com.ar", "Mercadolibre", encontrados)
        except Exception as e:
            log(f"ERROR Mercado Libre: {str(e)[:150]}")
            anotar("Mercadolibre", f"error en la API: {str(e)[:150]}")

    if config.get("busquedas"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(BASE / "perfil_navegador"), headless=True, locale="es-AR", user_agent=UA,
                viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            for busq in config["busquedas"]:
                nombre = tienda_de(busq)
                try:
                    links = links_de_busqueda(page, busq)
                except Exception as e:
                    log(f"ERROR búsqueda {busq}: {str(e)[:150]}")
                    anotar(nombre, f"error en el buscador: {str(e)[:150]}")
                    continue
                if links is None:
                    log(f"{nombre} (buscador) | bloqueado")
                    if nombre not in conocidas:
                        anotar(nombre, "la tienda bloquea el acceso")
                    continue
                encontrados = []
                for url, _ in links:
                    try:
                        r = revisar_pagina(page, url, pisos)
                    except Exception as e:
                        log(f"ERROR {url}: {str(e)[:150]}")
                        continue
                    if r.get("modelo"):
                        encontrados.append({**r, "url": url})
                anotar(nombre)
                resultados += [(nombre, r) for r in encontrados]
                marcar_no_publicado(busq, nombre, encontrados)
            ctx.close()

    for tienda, r in resultados:
        prod = productos[r["modelo"]]
        if r["estado"] == "disponible" and r["precio"] and r["precio"] < pisos[r["modelo"]]:
            continue  # accesorio que se coló, o precio mal cargado
        url = r["url"]
        log(f"{prod['nombre']} | {tienda} | {r['estado']} | {fmt(r['precio'])} | {texto_cuotas(r.get('cuotas'))} | {r.get('titulo', '')[:60]}")
        previo = estado_previo.get(url, {})
        estado_nuevo[url] = {"estado": r["estado"], "precio": r["precio"], "cuotas": r.get("cuotas"),
                             "titulo": r.get("titulo"), "vendedor": r.get("vendedor"),
                             "producto": prod["nombre"], "visto": visto}
        if r["estado"] != "disponible":
            continue
        if r["precio"]:
            filas_historial.append([visto, prod["nombre"], tienda, r["precio"], url])
        barato = r["precio"] is not None and r["precio"] <= prod["precio_max"]
        if not barato and not config.get("avisar_aunque_supere_precio_max", True):
            continue
        volvio_stock = previo.get("estado") != "disponible"
        bajo_precio = r["precio"] and previo.get("precio") and r["precio"] < previo["precio"]
        if volvio_stock or bajo_precio:
            motivo = "🟢 HAY STOCK" if volvio_stock else "📉 BAJÓ DE PRECIO"
            if r["precio"] is None:
                marca = "💲 revisá el precio en la página"
            elif barato:
                marca = "✅ debajo de tu tope"
            else:
                marca = f"⚠️ arriba de tu tope ({fmt(prod['precio_max'])})"
            cuotas = f"\n💳 {texto_cuotas(r['cuotas'])}" if r.get("cuotas") else ""
            vende = f" (vende {html.escape(r['vendedor'])})" if r.get("vendedor") and \
                sin_acentos(r["vendedor"])[:5] not in sin_acentos(tienda) else ""
            if IMPORTADA.search(sin_acentos(r.get("titulo") or "")):
                marca += "\n🌎 parece importada: puede no tener garantía oficial en Argentina"
            contexto = contexto_precio(historial, prod["nombre"], tienda, r["precio"])
            grafico = f"\n📈 {config['grafico']}" if config.get("grafico") else ""
            avisos.append((prod["modelo"], r["precio"] or 0,
                           f"🎮 {motivo}: <b>{prod['nombre']}</b>\n{tienda}{vende}: {fmt(r['precio'])} {marca}"
                           f"{cuotas}\n<i>{html.escape(r.get('titulo') or '')}</i>{contexto}\n{url}{grafico}"))

    # en las tiendas que anduvieron, lo que no apareció hoy ya no está publicado
    estado_nuevo = {u: v for u, v in estado_nuevo.items()
                    if v.get("visto") == visto or salud_corrida.get(tienda_de(u), "falló") is not None}
    salud, avisos_salud = actualizar_salud(salud_corrida)
    # si una tienda avisa muchas publicaciones a la vez, manda solo las 3 más baratas por modelo
    por_modelo = {}
    for modelo, precio, texto in sorted(avisos, key=lambda a: a[1]):
        por_modelo.setdefault(modelo, []).append(texto)
    mensajes = [t for lista in por_modelo.values() for t in lista[:3]]
    primera_vez = not estado_previo
    if primera_vez:  # todo es "nuevo": en vez de un aviso por publicación, un solo resumen
        mensajes = ["🎮 Monitor de PS5 conectado. Desde ahora te aviso cuando haya stock nuevo o baje un precio."]

    if dry_run:
        print("\n--- Avisos que se mandarían ---\n" + ("\n\n".join(mensajes + avisos_salud) or "(ninguno)"))
        print("\n--- Salud de esta corrida ---\n" + json.dumps(salud_corrida, ensure_ascii=False, indent=2))
        guardar_json(BASE / "state_prueba.json", estado_nuevo)
        return
    anotar_historial(filas_historial)
    guardar_json(SALUD, salud)
    for a in mensajes + avisos_salud:
        enviar_telegram(a)
    if con_resumen or primera_vez:
        enviar_telegram(resumen({u: v for u, v in estado_nuevo.items() if v.get("visto") == visto}))
    guardar_json(STATE, estado_nuevo)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args:
        enviar_telegram("🎮 Prueba del monitor de PS5: funciona.")
    else:
        main(dry_run="--dry-run" in args, con_resumen="--resumen" in args)
