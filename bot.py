#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PropAlertBot - Detecta propiedades nuevas en bancos CR y notifica por Telegram.
Corre gratis en GitHub Actions con state.json.

Mejoras incluidas:
- BN: parseo más exacto (precio "Valor informativo", provincia correcta, menos duplicados)
- BCR: parser mejorado (precio, provincia, dedupe)
- Telegram: Markdown + sin preview
- state.json limitado para no crecer infinito
- safe_get con reintentos
"""

import os
import json
import hashlib
import time
import re
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup


# -------------------------
# Configuración
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"
MAX_SEEN = 3000  # evita crecimiento infinito del state.json

USER_AGENT = "PropAlertBot/1.2 (+https://github.com/tu-usuario/tu-repo)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
    "Connection": "close",
}

# Bancos y URLs
BANKS = [
    {"name": "Banco Nacional", "url": "https://ventadebienes.bncr.fi.cr/propiedades"},
    {"name": "BCR - Casas", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/Casas?tipo_propiedad=1"},
    {"name": "BCR - Terrenos", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/terrenos?tipo_propiedad=3"},
    {"name": "Banco Popular", "url": "https://srv.bancopopular.fi.cr/Wb_BA_SharepointU/"},
    {"name": "BAC", "url": "https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas"},
    {"name": "Scotiabank", "url": "https://www.davibank.cr/homeshow/casas.aspx"},
    {"name": "BienesAdjudicadosCR", "url": "https://bienesadjudicadoscr.com/propiedades/"},
]


# -------------------------
# Utilidades
# -------------------------
PROVINCES = ["San José", "San Jose", "Alajuela", "Cartago", "Heredia", "Guanacaste", "Puntarenas", "Limón", "Limon"]

def normalize_url(u: str) -> str:
    """Normaliza URL para deduplicar (sin query/fragment y sin slash final)."""
    try:
        parts = requests.utils.urlparse(u)
        clean = parts._replace(query="", fragment="").geturl()
        return clean.rstrip("/")
    except Exception:
        return u.rstrip("/")

def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"seen": []}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"seen": []}

def save_state(path: str, state: Dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def make_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def safe_get(url: str, timeout: int = 20, retries: int = 2) -> str:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            print(f"Error GET {url} (intento {attempt+1}/{retries+1}): {e}")
            time.sleep(2)
    print("Fallo definitivo GET:", url, last_err)
    return ""

def send_telegram(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        print("Telegram token/chat_id no configurados; omitiendo envío.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        r.raise_for_status()
        print("Telegram enviado.")
    except Exception as e:
        print("Error enviando Telegram:", e)


# -------------------------
# Extractores (genéricos)
# -------------------------
def extract_price(text: str) -> str:
    m = re.search(r"(₡\s?[\d\.,]+|\bCRC\s?[\d\.,]+|\$\s?[\d\.,]+)", text)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"(\d{1,3}(?:[.,]\d{3})+)", text)
    if m2:
        return m2.group(1).strip()
    return ""

def extract_size(text: str) -> str:
    m = re.search(r"(\d{1,7})\s*(m2|m²)", text, re.IGNORECASE)
    if m:
        return f"{m.group(1)} m²"
    return ""

def extract_province(text: str) -> str:
    t = text.lower()
    for p in PROVINCES:
        if p.lower() in t:
            # normalizar "San Jose" -> "San José", "Limon" -> "Limón"
            if p.lower() == "san jose":
                return "San José"
            if p.lower() == "limon":
                return "Limón"
            return p
    return ""

def extract_code_generic(text: str) -> str:
    """Códigos comunes tipo 9809-1"""
    m = re.search(r"\b(\d{3,6}-\d)\b", text)
    return m.group(1) if m else ""


# -------------------------
# Extractores específicos BN
# -------------------------
def extract_bn_price(text: str) -> str:
    # "Valor informativo: ₡..."
    m = re.search(r"Valor\s+informativo:\s*(₡\s*[\d\.,]+|\$\s*[\d\.,]+|\bCRC\s*[\d\.,]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return extract_price(text)

def extract_bn_province(text: str) -> str:
    """
    BN suele mostrar ubicación como: "CARTAGO, ALVARADO, CAPELLADES"
    Tomamos SOLO la provincia (primer segmento antes de coma).
    """
    # capturar "MAYUSCULAS, ..." (con tildes)
    m = re.search(r"\b([A-ZÁÉÍÓÚÑ ]+)\s*,", text)
    if m:
        prov = " ".join(m.group(1).split()).title()
        # normalizaciones típicas:
        if prov.lower() == "san jose":
            return "San José"
        if prov.lower() == "limon":
            return "Limón"
        # puede venir "San José" sin tilde
        return prov
    # fallback a lista
    return extract_province(text)

def extract_bn_code(text: str, url: str = "") -> str:
    code = extract_code_generic(text)
    if code:
        return code
    if url:
        code2 = extract_code_generic(url)
        if code2:
            return code2
    return ""


# -------------------------
# Extractores específicos BCR
# -------------------------
def extract_bcr_price(text: str) -> str:
    # BCR suele tener "Precio: ¢ 111.939.300,00" (a veces con ¢)
    m = re.search(r"Precio\s*:?\s*(¢\s*[\d\.,]+|₡\s*[\d\.,]+|\bCRC\s*[\d\.,]+|\$\s*[\d\.,]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # fallback genérico
    return extract_price(text)

def extract_bcr_province(text: str) -> str:
    # Muchas páginas BCR muestran provincia/cantón en mayúsculas: "ALAJUELA ALAJUELA" o "PUNTARENAS GOLFITO"
    # Tomamos el primer token provincia si coincide con catálogo.
    # 1) Intento con lista normal:
    prov = extract_province(text)
    if prov:
        return prov
    # 2) Intento con mayúsculas sin tildes:
    up = re.findall(r"\b([A-ZÁÉÍÓÚÑ]{4,})\b", text)
    # mapea posibles provincias
    mapping = {
        "SAN": "San José",  # no ideal, pero evitamos falsos con 2 tokens; lo manejamos mejor abajo
        "JOSE": "San José",
        "ALAJUELA": "Alajuela",
        "CARTAGO": "Cartago",
        "HEREDIA": "Heredia",
        "GUANACASTE": "Guanacaste",
        "PUNTARENAS": "Puntarenas",
        "LIMON": "Limón",
        "LIMÓN": "Limón",
    }
    # buscar tokens exactos de provincia
    for token in up:
        if token in mapping and token not in ("SAN", "JOSE"):
            return mapping[token]
    # caso especial "SAN JOSE"
    if "SAN" in up and "JOSE" in up:
        return "San José"
    return ""

def extract_bcr_code(text: str, url: str = "") -> str:
    # Códigos tipo BCR-BA-7802490326 o BCR-BA1024520721
    m = re.search(r"\b(BCR-BA-?\d{6,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # también puede haber folio real (no siempre único para publicar, pero útil)
    m2 = re.search(r"Folio\s+real\s*:?\s*([\d\-]+)", text, re.IGNORECASE)
    if m2:
        return f"FOLIO:{m2.group(1)}"
    # fallback código genérico 9809-1 si existiera
    code = extract_code_generic(text)
    if code:
        return code
    if url:
        code2 = extract_code_generic(url)
        if code2:
            return code2
    return ""


# -------------------------
# Parsers
# -------------------------
def parse_bn(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")

    anchors = soup.find_all("a", href=True)
    seen_local = set()

    for a in anchors:
        href = a["href"].strip()
        if "/propiedades/" not in href:
            continue

        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        full = normalize_url(full)

        # encontrar contenedor cercano que incluya "Valor informativo"
        container = None
        node = a
        for _ in range(8):
            node = node.parent
            if not node:
                break
            txt = node.get_text(" ", strip=True)
            if "Valor informativo" in txt:
                container = node
                break

        snippet = container.get_text(" ", strip=True) if container else a.get_text(" ", strip=True)

        # título preferido
        title = ""
        if container:
            h = container.find(["h1", "h2", "h3", "h4"])
            if h:
                title = h.get_text(" ", strip=True)
        if not title:
            title = a.get("title") or a.get_text(" ", strip=True) or snippet[:120]

        province = extract_bn_province(snippet)
        price = extract_bn_price(snippet)
        code = extract_bn_code(snippet, full)

        # id estable por código si existe, si no por URL
        stable = f"BN:{code}" if code else f"BNURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        items.append({
            "url": full,
            "title": title,
            "location": province,       # SOLO provincia
            "size": extract_size(snippet),
            "price": price,
            "id": item_id
        })

    return items


def parse_bcr(url: str) -> List[Dict[str, str]]:
    """
    Parser mejorado BCR:
    - Busca "tarjetas" por texto que contenga "Precio"
    - Dentro de cada tarjeta toma el mejor link (oferta/compra/detalle)
    - Extrae precio con etiqueta "Precio:"
    - Extrae SOLO provincia
    - Dedupe por código BCR si existe, sino URL normalizada
    """
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    seen_local = set()

    # 1) Encuentra bloques candidatos (div/section/article) cuyo texto tenga "Precio"
    candidates = soup.find_all(lambda tag: tag.name in ["div", "section", "article", "li"] and "Precio" in tag.get_text(" ", strip=True))

    # 2) Si no hay candidatos, fallback: buscar anchors
    if not candidates:
        candidates = soup.find_all("a", href=True)

    for block in candidates:
        block_text = block.get_text(" ", strip=True)

        # Evitar bloques gigantes (navegación) que no describen una propiedad
        if "Precio" not in block_text and not isinstance(block, type(soup.a)):
            continue

        # intenta encontrar link dentro del bloque
        a = block.find("a", href=True) if hasattr(block, "find") else (block if getattr(block, "name", "") == "a" else None)
        if not a or not a.get("href"):
            continue

        href = a["href"].strip()
        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        full = normalize_url(full)

        # Título: usar texto del link o algo del bloque
        title = a.get_text(" ", strip=True) or a.get("title") or ""
        if not title:
            # intenta encontrar encabezado cercano
            h = block.find(["h1", "h2", "h3", "h4"]) if hasattr(block, "find") else None
            title = h.get_text(" ", strip=True) if h else block_text[:120]

        price = extract_bcr_price(block_text)
        province = extract_bcr_province(block_text)
        code = extract_bcr_code(block_text, full)

        stable = f"BCR:{code}" if code else f"BCRURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        # Filtro: evita links que no parecen propiedad (muy genérico)
        # (puedes comentar esto si sientes que está filtrando de más)
        if len(title) < 3 and "Precio" not in block_text:
            continue

        items.append({
            "url": full,
            "title": title,
            "location": province,        # SOLO provincia
            "size": extract_size(block_text),
            "price": price,
            "id": item_id
        })

    return items


def parse_popular(url: str) -> List[Dict[str, str]]:
    # Portal del Popular: a veces se puede leer HTML directo. Mantenemos heurístico.
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_local = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["/sites/", "/bienes", "/propiedades", "/detalle"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            full = normalize_url(full)
            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            province = extract_province(snippet)
            item_id = make_id(f"POPULAR:{full}")

            if item_id in seen_local:
                continue
            seen_local.add(item_id)

            items.append({
                "url": full,
                "title": title,
                "location": province,
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": item_id
            })
    return items


def parse_bac(url: str) -> List[Dict[str, str]]:
    # BAC frecuentemente usa JS; mantenemos heurístico
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)



