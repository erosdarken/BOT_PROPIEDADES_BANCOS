#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PropAlertBot - Detecta propiedades nuevas en bancos CR y notifica por Telegram.
Diseñado para correr gratis en GitHub Actions + state.json.
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

# Evita crecimiento infinito del state.json
MAX_SEEN = 3000

# User-Agent: cámbialo por tu repo real si quieres (recomendado)
USER_AGENT = "PropAlertBot/1.1 (+https://github.com/tu-usuario/tu-repo)"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
    "Connection": "close",
}

# Bancos y URLs
BANKS = [
    {"name": "Banco Nacional", "url": "https://ventadebienes.bncr.fi.cr/propiedades"},
    # ✅ Corregidas: sin &amp; (HTML) y sin & extra
    {"name": "BCR - Casas", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/Casas?tipo_propiedad=1"},
    {"name": "BCR - Terrenos", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/terrenos?tipo_propiedad=3"},
    {"name": "Banco Popular", "url": "https://srv.bancopopular.fi.cr/Wb_BA_SharepointU/"},
    {"name": "BAC", "url": "https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas"},
    # Nota: esta URL no es de Scotiabank; la dejo como estaba en tu lista
    {"name": "Scotiabank", "url": "https://www.davibank.cr/homeshow/casas.aspx"},
    {"name": "BienesAdjudicadosCR", "url": "https://bienesadjudicadoscr.com/propiedades/"},
]

# -------------------------
# Utilidades de estado
# -------------------------
def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"seen": []}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # Si se corrompe por algún motivo, reinicia
            return {"seen": []}

def save_state(path: str, state: Dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def make_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

# -------------------------
# Telegram
# -------------------------
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
# HTTP robusto
# -------------------------
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

# -------------------------
# Extractores (heurísticos)
# -------------------------
def extract_size(text: str) -> str:
    """
    Busca tamaños tipo: "350 m2", "350m2", "350 m²"
    """
    m = re.search(r"(\d{1,7})\s*(m2|m²)", text, re.IGNORECASE)
    if m:
        return f"{m.group(1)} m²"
    return ""

def extract_price(text: str) -> str:
    """
    Busca precios tipo: ₡ 10.000.000 / CRC 10000000 / $ 100,000
    """
    m = re.search(r"(₡\s?[\d\.,]+|\bCRC\s?[\d\.,]+|\$\s?[\d\.,]+)", text)
    if m:
        return m.group(1)

    # fallback: números grandes con separadores
    m2 = re.search(r"(\d{1,3}(?:[.,]\d{3})+)", text)
    if m2:
        return m2.group(1)
    return ""

def extract_location(text: str) -> str:
    provinces = ["Cartago", "San José", "San Jose", "Heredia", "Alajuela", "Puntarenas", "Limón", "Limon"]
    for p in provinces:
        if p.lower() in text.lower():
            return p

    m = re.search(r"Ubicaci[oó]n[:\-]\s*([^,;\n]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    m2 = re.search(r"(Provincia|Cant[oó]n)[:\-]\s*([^,;\n]+)", text, re.IGNORECASE)
    if m2:
        return m2.group(2).strip()

    return ""

# -------------------------
# Parsers específicos (heurísticos)
# -------------------------
def parse_bn(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")

    # Heurística: tarjetas con 'card' o 'prop'
    cards = soup.find_all(
        lambda tag: tag.name in ["article", "div"]
        and tag.get("class")
        and any("card" in c.lower() or "prop" in c.lower() for c in tag.get("class"))
    )

    if not cards:
        # Fallback: buscar enlaces de detalle
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = a["href"]
            if any(k in href.lower() for k in ["/prop", "/detalle", "/ficha", "/inmueble"]):
                title = a.get_text(strip=True) or a.get("title") or ""
                full = href if href.startswith("http") else requests.compat.urljoin(url, href)
                snippet = a.parent.get_text(" ", strip=True) if a.parent else title
                items.append({
                    "url": full,
                    "title": title,
                    "location": extract_location(snippet),
                    "size": extract_size(snippet),
                    "price": extract_price(snippet),
                    "id": make_id(full + title),
                })
        return items

    for c in cards:
        a = c.find("a", href=True)
        title = (a.get_text(" ", strip=True) or a.get("title") or "") if a else c.get_text(" ", strip=True)[:120]
        href = a["href"] if a else ""
        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        snippet = c.get_text(" ", strip=True)
        items.append({
            "url": full or url,
            "title": title,
            "location": extract_location(snippet),
            "size": extract_size(snippet),
            "price": extract_price(snippet),
            "id": make_id((full or url) + title),
        })
    return items

def parse_bcr(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_urls = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["/detalle", "/ficha", "bienes"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen_urls:
                continue
            seen_urls.add(full)

            title = a.get_text(" ", strip=True) or a.get("title") or ""
            parent = a.parent
            snippet = parent.get_text(" ", strip=True) if parent else title

            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title),
            })
    return items

def parse_popular(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_urls = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["/sites/", "/bienes", "/propiedades", "/detalle"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen_urls:
                continue
            seen_urls.add(full)

            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title

            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title),
            })
    return items

def parse_bac(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_urls = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["vivienda", "detalle", "/propiedad", "/inmueble"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen_urls:
                continue
            seen_urls.add(full)

            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title

            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title),
            })
    return items

def parse_scotiabank(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_urls = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["casas", "detalle", "propiedad", "ficha"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen_urls:
                continue
            seen_urls.add(full)

            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title

            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title),
            })
    return items

def parse_bienesadjudicados(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)

    seen_urls = set()
    for a in anchors:
        href = a["href"]
        if "/propiedades/" in href or "/propiedad/" in href or "propiedades" in href.lower():
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen_urls:
                continue
            seen_urls.add(full)

            title = a.get_text(" ", strip=True) or a.get("title") or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title

            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title),
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
    state = load_state(STATE_FILE)
    seen = set(state.get("seen", []))
    new_seen = set(seen)
    new_items: List[Dict[str, str]] = []

    for bank in BANKS:
        name = bank["name"]
        url = bank["url"]
        print(f"Comprobando {name} -> {url}")

        parser = PARSERS.get(name)
        items = parser(url) if parser else []

        print(f"  Candidatos encontrados: {len(items)}")

        for it in items:
            item_id = it.get("id") or make_id((it.get("url", "") + it.get("title", "")))
            if item_id in seen:
                continue

            new_items.append({"bank": name, **it, "id": item_id})
            new_seen.add(item_id)

    # Enviar notificaciones por Telegram
    if new_items:
        print(f"Se detectaron {len(new_items)} propiedades nuevas. Enviando por Telegram...")
    else:
        print("No hay nuevos items.")

    for it in new_items:
        msg = (
            "🚨 *Propiedad nueva detectada*\n\n"
            f"🏦 *Banco:* {it.get('bank')}\n"
            f"📍 *Ubicación:* {it.get('location') or '_No disponible_'}\n"
            f"📐 *Tamaño:* {it.get('size') or '_No disponible_'}\n"
            f"💰 *Precio:* {it.get('price') or '_No disponible_'}\n"
            f"🔗 {it.get('url')}\n"
        )
        print("Enviando:", it.get("url"))
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
        time.sleep(1)  # pequeño rate-limit

    # Actualizar estado si hay nuevos
    if new_seen != seen:
        # Limitar tamaño del historial
        state["seen"] = list(new_seen)[-MAX_SEEN:]
        save_state(STATE_FILE, state)
        print("Estado actualizado:", len(new_seen) - len(seen), "nuevos.")
    else:
        print("Estado sin cambios.")

if __name__ == "__main__":
    main()

