#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PropAlertBot - Detecta propiedades nuevas en bancos CR y notifica por Telegram.
Corre gratis en GitHub Actions con state.json.

Incluye modo debug:
- Logs sin buffering (flush=True)
- Conteo por banco
- Ejemplo del primer item por banco
- Debug HTTP opcional (status code + tamaño HTML)
- Resumen por Telegram SOLO en ejecución manual (workflow_dispatch)
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
GITHUB_EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "")

STATE_FILE = "state.json"
MAX_SEEN = 3000  # evita crecimiento infinito del state.json

# Debug (actívalo poniendo DEBUG_HTTP=1 en el workflow si lo necesitas)
DEBUG_HTTP = os.getenv("DEBUG_HTTP", "0") == "1"

# Opcional: limitar cuantos mensajes envía por corrida (útil si reseteas state)
MAX_SEND = int(os.getenv("MAX_SEND", "0"))  # 0 = sin límite

USER_AGENT = "PropAlertBot/1.3 (+https://github.com/tu-usuario/tu-repo)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
    "Connection": "close",
    # A veces ayuda a evitar bloqueos:
    "Referer": "https://www.google.com/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

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

def safe_get(url: str, timeout: int = 25, retries: int = 3) -> str:
    last_err: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if DEBUG_HTTP:
                print(f"[HTTP] GET {url} -> {r.status_code} ({len(r.text)} chars)", flush=True)

            # Si es OK y tiene HTML, lo devolvemos
            if 200 <= r.status_code < 400 and r.text:
                return r.text

            # Si no es OK, esperamos y reintentamos
            time.sleep(3 + attempt * 2)
        except Exception as e:
            last_err = e
            print(f"[HTTP] Error GET {url} (intento {attempt+1}/{retries+1}): {e}", flush=True)
            time.sleep(3 + attempt * 2)

    print("[HTTP] Fallo definitivo GET:", url, last_err, flush=True)
    return ""

def send_telegram(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        print("[TG] Telegram token/chat_id no configurados; omitiendo envío.", flush=True)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        r = SESSION.post(url, data=payload, timeout=20)
        r.raise_for_status()
        print("[TG] Telegram enviado.", flush=True)
    except Exception as e:
        print("[TG] Error enviando Telegram:", e, flush=True)


# -------------------------
# Extractores (genéricos)
# -------------------------
def extract_price(text: str) -> str:
    m = re.search(r"(₡\s?[\d\.,]+|\bCRC\s?[\d\.,]+|¢\s?[\d\.,]+|\$\s?[\d\.,]+)", text)
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
            if p.lower() == "san jose":
                return "San José"
            if p.lower() == "limon":
                return "Limón"
            return p
    return ""

def extract_code_generic(text: str) -> str:
    m = re.search(r"\b(\d{3,6}-\d)\b", text)
    return m.group(1) if m else ""


# -------------------------
# Extractores específicos BN
# -------------------------
def extract_bn_price(text: str) -> str:
    m = re.search(r"Valor\s+informativo:\s*(₡\s*[\d\.,]+|\$\s*[\d\.,]+|\bCRC\s*[\d\.,]+|¢\s*[\d\.,]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return extract_price(text)

def extract_bn_province(text: str) -> str:
    # BN suele mostrar "CARTAGO, ALVARADO, CAPELLADES" -> nos quedamos con "Cartago"
    m = re.search(r"\b([A-ZÁÉÍÓÚÑ ]+)\s*,", text)
    if m:
        prov = " ".join(m.group(1).split()).title()
        if prov.lower() == "san jose":
            return "San José"
        if prov.lower() == "limon":
            return "Limón"
        return prov
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
    m = re.search(r"Precio\s*:?\s*(¢\s*[\d\.,]+|₡\s*[\d\.,]+|\bCRC\s*[\d\.,]+|\$\s*[\d\.,]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return extract_price(text)

def extract_bcr_province(text: str) -> str:
    prov = extract_province(text)
    if prov:
        return prov

    up = re.findall(r"\b([A-ZÁÉÍÓÚÑ]{4,})\b", text)
    mapping = {
        "ALAJUELA": "Alajuela",
        "CARTAGO": "Cartago",
        "HEREDIA": "Heredia",
        "GUANACASTE": "Guanacaste",
        "PUNTARENAS": "Puntarenas",
        "LIMON": "Limón",
        "LIMÓN": "Limón",
        "SAN": "San José",
        "JOSE": "San José",
        "JOSÉ": "San José",
    }

    for token in up:
        if token in mapping and token not in ("SAN", "JOSE", "JOSÉ"):
            return mapping[token]
    if "SAN" in up and ("JOSE" in up or "JOSÉ" in up):
        return "San José"
    return ""

def extract_bcr_code(text: str, url: str = "") -> str:
    m = re.search(r"\b(BCR-BA-?\d{6,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    m2 = re.search(r"Folio\s+real\s*:?\s*([\d\-]+)", text, re.IGNORECASE)
    if m2:
        return f"FOLIO:{m2.group(1)}"

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

        # contenedor cercano con "Valor informativo"
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

        stable = f"BN:{code}" if code else f"BNURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        items.append({
            "url": full,
            "title": title,
            "location": province,  # SOLO provincia
            "size": extract_size(snippet),
            "price": price,
            "id": item_id
        })

    return items


def parse_bcr(url: str) -> List[Dict[str, str]]:
    """
    Parser mejorado para BCR:
    - Detecta bloques que contengan "Precio"
    - Extrae link, precio, provincia
    - Deduplica por código BCR si existe
    """
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    if DEBUG_HTTP:
        print("[BCR] HTML contiene 'Precio'?:", ("Precio" in html), flush=True)
        print("[BCR] HTML contiene 'BCR-BA'?:", ("BCR-BA" in html), flush=True)

    soup = BeautifulSoup(html, "html.parser")
    seen_local = set()

    candidates = soup.find_all(
        lambda tag: tag.name in ["div", "section", "article", "li"]
        and "Precio" in tag.get_text(" ", strip=True)
    )

    if not candidates:
        # fallback suave: buscar anchors
        candidates = soup.find_all("a", href=True)

    for block in candidates:
        block_text = block.get_text(" ", strip=True) if hasattr(block, "get_text") else ""
        if "Precio" not in block_text and getattr(block, "name", "") != "a":
            continue

        a = block.find("a", href=True) if hasattr(block, "find") else None
        if getattr(block, "name", "") == "a":
            a = block

        if not a or not a.get("href"):
            continue

        href = a["href"].strip()
        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        full = normalize_url(full)

        title = a.get_text(" ", strip=True) or a.get("title") or ""
        if not title:
            h = block.find(["h1", "h2", "h3", "h4"]) if hasattr(block, "find") else None
            title = h.get_text(" ", strip=True) if h else (block_text[:120] if block_text else full)

        price = extract_bcr_price(block_text)
        province = extract_bcr_province(block_text)
        code = extract_bcr_code(block_text, full)

        stable = f"BCR:{code}" if code else f"BCRURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        items.append({
            "url": full,
            "title": title,
            "location": province,  # SOLO provincia
            "size": extract_size(block_text),
            "price": price,
            "id": item_id
        })

    return items


def parse_popular(url: str) -> List[Dict[str, str]]:
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
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_local = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["vivienda", "detalle", "/propiedad", "/inmueble"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            full = normalize_url(full)
            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            province = extract_province(snippet)
            item_id = make_id(f"BAC:{full}")

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


def parse_scotiabank(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_local = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["casas", "detalle", "propiedad", "ficha"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            full = normalize_url(full)
            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            province = extract_province(snippet)
            item_id = make_id(f"SCOTIA:{full}")

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


def parse_bienesadjudicados(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_local = set()
    for a in anchors:
        href = a["href"]
        if "/propiedades/" in href or "/propiedad/" in href or "propiedades" in href.lower():
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            full = normalize_url(full)
            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            province = extract_province(snippet)
            item_id = make_id(f"BADJ:{full}")

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


PARSERS = {
    "Banco Nacional": parse_bn,
    "BCR - Casas": parse_bcr,
    "BCR - Terrenos": parse_bcr,
    "Banco Popular": parse_popular,
    "BAC": parse_bac,
    "Scotiabank": parse_scotiabank,
    "BienesAdjudicadosCR": parse_bienesadjudicados,
}


# -------------------------
# Flujo principal
# -------------------------
def main():
    print("✅ Iniciando PropAlertBot...", flush=True)

    state = load_state(STATE_FILE)
    seen = set(state.get("seen", []))
    new_seen = set(seen)
    new_items: List[Dict[str, str]] = []

    counts: Dict[str, int] = {}

    for bank in BANKS:
        name = bank["name"]
        url = bank["url"]
        print(f"\nComprobando {name} -> {url}", flush=True)

        parser = PARSERS.get(name)
        items = parser(url) if parser else []
        counts[name] = len(items)

        print(f"  Candidatos encontrados: {len(items)}", flush=True)
        if items:
            sample = items[0]
            print(f"  Ejemplo: title='{sample.get('title')}' | price='{sample.get('price')}' | prov='{sample.get('location')}'", flush=True)

        for it in items:
            item_id = it.get("id") or make_id((it.get("url", "") + it.get("title", "")))
            if item_id in seen:
                continue
            new_items.append({"bank": name, **it, "id": item_id})
            new_seen.add(item_id)

    # En ejecución manual, manda resumen SIEMPRE (para debug sin mirar logs)
    if GITHUB_EVENT_NAME == "workflow_dispatch":
        resumen = "📊 *Resumen de scraping*\n\n" + "\n".join([f"- {k}: {v}" for k, v in counts.items()])
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, resumen)

    if not new_items:
        print("\nNo hay nuevos items.", flush=True)
    else:
        print(f"\nSe detectaron {len(new_items)} propiedades nuevas.", flush=True)

    # Límite opcional de envío
    if MAX_SEND > 0:
        new_items = new_items[:MAX_SEND]
        print(f"Aplicando MAX_SEND={MAX_SEND}. Enviaré {len(new_items)} mensajes.", flush=True)

    # Enviar notificaciones
    for it in new_items:
        province = it.get("location") or "No disponible"
        msg = (
            "🚨 *Propiedad nueva detectada*\n\n"
            f"🏦 *Banco:* {it.get('bank')}\n"
            f"🗺️ *Provincia:* {province}\n"
            f"📐 *Tamaño:* {it.get('size') or '_No disponible_'}\n"
            f"💰 *Precio:* {it.get('price') or '_No disponible_'}\n"
            f"🔗 {it.get('url')}\n"
        )
        print("Enviando:", it.get("url"), flush=True)
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
        time.sleep(1)

    # Guardar estado
    if new_seen != seen:
        state["seen"] = list(new_seen)[-MAX_SEEN:]
        save_state(STATE_FILE, state)
        print(f"Estado actualizado: {len(new_seen) - len(seen)} nuevos.", flush=True)
    else:
        print("Estado sin cambios.", flush=True)

    print("✅ PropAlertBot terminó ejecución.", flush=True)


if __name__ == "__main__":
    main()
``


