"""Publica el monitor de PS5 en GitHub Actions y mantiene sus secretos.

Uso (en tu PC):
  python nube.py publicar     crea el repo, sube los archivos, carga los secretos, activa la página y lo corre una vez
  python nube.py actualizar   vuelve a subir los archivos (después de cambiar config.json o el código)
  python nube.py token        reemplaza el token de GitHub cuando está por vencer

Uso interno (lo corre GitHub Actions):
  python nube.py aviso-corrida     avisa por Telegram si la nube empezó a fallar o si el token vence pronto
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
SECRETS = BASE / "secrets.json"
SALUD = BASE / "salud.json"
REPO_NOMBRE = "monitor-ps5"
ARCHIVOS = ["monitor.py", "nube.py", "config.json", "requirements.txt", "LEEME.md", "index.html",
            ".github/workflows/monitor.yml", "manifest.webmanifest",
            "img/ps5-standard.webp", "img/ps5-digital.webp", "img/ps5-pro.webp",
            "img/icon-192.png", "img/icon-512.png", "img/icon-maskable-512.png", "img/apple-touch-icon.png",
            "img/favicon-32.png"]
DIAS_AVISO_TOKEN = 14


def gh(metodo, ruta, token, datos=None):
    req = urllib.request.Request(
        f"https://api.github.com{ruta}", method=metodo,
        data=json.dumps(datos).encode() if datos is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            cuerpo = r.read().decode()
            return json.loads(cuerpo) if cuerpo else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and metodo == "GET":
            return None
        raise SystemExit(f"GitHub respondió {e.code} en {metodo} {ruta}: {e.read().decode()[:300]}")


def poner_secreto(token, repo, nombre, valor):
    from nacl import encoding, public
    clave = gh("GET", f"/repos/{repo}/actions/secrets/public-key", token)
    caja = public.SealedBox(public.PublicKey(clave["key"].encode(), encoding.Base64Encoder()))
    cifrado = base64.b64encode(caja.encrypt(str(valor).encode())).decode()
    gh("PUT", f"/repos/{repo}/actions/secrets/{nombre}", token,
       {"encrypted_value": cifrado, "key_id": clave["key_id"]})


def subir_archivos(token, repo):
    for ruta in ARCHIVOS:
        contenido = base64.b64encode((BASE / ruta).read_bytes()).decode()
        actual = gh("GET", f"/repos/{repo}/contents/{ruta}", token)
        datos = {"message": f"actualizar {ruta}", "content": contenido}
        if actual:
            if actual.get("content", "").replace("\n", "") == contenido:
                continue  # sin cambios
            datos["sha"] = actual["sha"]
        gh("PUT", f"/repos/{repo}/contents/{ruta}", token, datos)
        print("  subido:", ruta)


def leer_secretos():
    return json.loads(SECRETS.read_text(encoding="utf-8"))


def pedir_token():
    sec = leer_secretos()
    if sec.get("gh_pat"):
        return sec["gh_pat"]
    pat = input("Pegá el token de GitHub (empieza con ghp_) y Enter: ").strip()
    if not gh("GET", "/user", pat):
        raise SystemExit("Ese token no funciona.")
    sec["gh_pat"] = pat
    SECRETS.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
    return pat


def activar_pagina(token, usuario, repo):
    if not gh("GET", f"/repos/{repo}/pages", token):
        gh("POST", f"/repos/{repo}/pages", token, {"source": {"branch": "main", "path": "/"}})
        print(f"Página activada: https://{usuario}.github.io/{REPO_NOMBRE}/ (tarda unos minutos la primera vez)")


def publicar():
    token = pedir_token()
    usuario = gh("GET", "/user", token)["login"]
    repo = f"{usuario}/{REPO_NOMBRE}"
    if not gh("GET", f"/repos/{repo}", token):
        # público: GitHub Actions es gratis e ilimitado; los secretos quedan cifrados y no se ven
        gh("POST", "/user/repos", token, {"name": REPO_NOMBRE, "private": False, "auto_init": True,
                                          "description": "Monitor de stock, precio y cuotas de la PS5"})
        print("Repo creado:", repo)
    subir_archivos(token, repo)
    sec = leer_secretos()
    for nombre, valor in {"TELEGRAM_TOKEN": sec["token"], "TELEGRAM_CHAT_ID": sec["chat_id"],
                          "ML_CLIENT_ID": sec["ml_client_id"], "ML_SECRET": sec["ml_secret"],
                          "GH_PAT": token}.items():
        poner_secreto(token, repo, nombre, valor)
        print("  secreto cargado:", nombre)
    activar_pagina(token, usuario, repo)
    gh("POST", f"/repos/{repo}/actions/workflows/monitor.yml/dispatches", token, {"ref": "main"})
    print(f"\nListo. Primera corrida lanzada: https://github.com/{repo}/actions")


def actualizar():
    token = pedir_token()
    usuario = gh("GET", "/user", token)["login"]
    repo = f"{usuario}/{REPO_NOMBRE}"
    subir_archivos(token, repo)
    activar_pagina(token, usuario, repo)
    print("Archivos actualizados.")


def token_nuevo():
    sec = leer_secretos()
    sec.pop("gh_pat", None)
    SECRETS.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
    token = pedir_token()
    repo = f"{gh('GET', '/user', token)['login']}/{REPO_NOMBRE}"
    poner_secreto(token, repo, "GH_PAT", token)
    print("Token de GitHub reemplazado en tu PC y en la nube.")


def telegram(texto):
    sec = leer_secretos()
    datos = urllib.parse.urlencode({"chat_id": sec["chat_id"], "text": texto, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{sec['token']}/sendMessage", data=datos, timeout=20)


def aviso_corrida():
    repo, pat, resultado = os.environ["REPO"], os.environ["GH_PAT"], os.environ["RESULTADO"]
    runs = gh("GET", f"/repos/{repo}/actions/workflows/monitor.yml/runs?status=completed&per_page=1",
              pat)["workflow_runs"]
    anterior = runs[0]["conclusion"] if runs else "success"
    if resultado == "failure" and anterior != "failure":
        telegram(f"🔧 El monitor de PS5 falló en la nube. Detalle: "
                 f"https://github.com/{repo}/actions/runs/{os.environ['RUN_ID']}")
    elif resultado == "success" and anterior == "failure":
        telegram("✅ El monitor de PS5 volvió a andar en la nube.")
    # el aviso de vencimiento del token ya lo manda el monitor de teles (es el mismo token)


if __name__ == "__main__":
    {"publicar": publicar, "actualizar": actualizar, "token": token_nuevo,
     "aviso-corrida": aviso_corrida}[sys.argv[1]]()
