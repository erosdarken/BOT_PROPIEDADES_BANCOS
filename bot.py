#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PropAlertBot - Detecta propiedades nuevas en bancos CR y notifica por Telegram.

Mejoras:
- BN: mejor separación por "tarjeta" evitando contenedores gigantes (más de 1 resultado).
- BAC: parser específico para "Precio con Descuento" + botón "Ver más".
- Telegram: manejo de 429 Too Many Requests con retry_after.
- Debug: resumen por banco en workflow_dispatch.
"""

import os
import json
import hashlib
import time
import re
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup


# -------------------------
# Configuración
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GITHUB_EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "")

STATE_FILE = "state.json"
MAX_SEEN = 3000

DEBUG_HTTP = os.getenv("DEBUG_HTTP", "0") == "1"
MAX_SEND = int(os.getenv("MAX_SEND", "0"))  # 0 = sin límite (recomendado poner 10-20)
SLEEP_BETWEEN_MSG = float(os.getenv("SLEEP_BETWEEN_MSG", "1.2"))

USER_AGENT = "PropAlertBot/1.4 (+https://github.com/tu-usuario/tu-repo)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CR,es;q=0.9,en;q=0.8",
    "Connection": "close",
    "Referer": "https://www.google.com/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

PROVINCES = ["San José", "San Jose", "Alajuela", "Cartago", "Heredia", "Guanacaste", "Puntarenas", "Limón", "Limon"]

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
def normalize_url(u: str) -> str:
    try:
        parts = requests.utils.urlparse(u)
        clean = parts._replace(query="", fragment="").geturl()
        return clean.rstrip("/")
    except Exception:
        return u.rstrip("/")

def make_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

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

def safe_get(url: str, timeout: int = 25, retries: int = 3) -> str:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if DEBUG_HTTP:
                print(f"[HTTP] GET {url} -> {r.status_code} ({len(r.text)} chars)", flush=True)

            if 200 <= r.status_code < 400 and r.text:
                return r.text

            time.sleep(2 + attempt * 2)
        except Exception as e:
            last_err = e
            print(f"[HTTP] Error GET {url} (intento {attempt+1}/{retries+1}): {e}", flush=True)
            time.sleep(2 + attempt * 2)

    print("[HTTP] Fallo definitivo GET:", url, last_err, flush=True)
    return ""


# -------------------------
# Extractores
# -------------------------
def extract_price(text: str) -> str:
    m = re.search(r"(₡\s?[\d\.,]+|\bCRC\s?[\d\.,]+|¢\s?[\d\.,]+|\$\s?[\d\.,]+)", text)
    return m.group(1).strip() if m else ""

def extract_size(text: str) -> str:
    m = re.search(r"(\d{1,7})\s*(m2|m²)", text, re.IGNORECASE)
    return f"{m.group(1)} m²" if m else ""

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
# Telegram (con manejo 429)
# -------------------------
def send_telegram(token: str, chat_id: str, text: str, max_retries: int = 5):
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

    for attempt in range(max_retries):
        try:
            r = SESSION.post(url, data=payload, timeout=25)

            # Manejo 429: Telegram responde con retry_after
            if r.status_code == 429:
                try:
                    data = r.json()
                    retry_after = int(data.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    retry_after = 5
                wait = retry_after + 1
                print(f"[TG] 429 Too Many Requests. Esperando {wait}s y reintentando...", flush=True)
                time.sleep(wait)
                continue

            r.raise_for_status()
            print("[TG] Telegram enviado.", flush=True)
            return

        except Exception as e:
            print(f"[TG] Error enviando Telegram (intento {attempt+1}/{max_retries}): {e}", flush=True)
            time.sleep(2 + attempt * 2)

    print("[TG] No se pudo enviar tras varios reintentos.", flush=True)


# -------------------------
# BN (Banco Nacional) - FIX: más de 1 resultado
# -------------------------
def bn_best_container(a_tag) -> Tuple[Optional[Any], str]:
    """
    Sube por ancestros buscando un contenedor que parezca 'una sola tarjeta' de BN.
    Criterios:
    - Contenga "Valor informativo" (BN lo usa en el listado). [1](https://ventadebienes.bncr.fi.cr/)
    - Que no contenga múltiples "Ver detalle" / "Valor informativo" (evitar contenedores gigantes).
    """
    node = a_tag
    best = None
    best_text = ""

    for _ in range(12):
        node = node.parent
        if not node:
            break
        txt = node.get_text(" ", strip=True)

        # debe tener la etiqueta BN
        if "Valor informativo" not in txt:
            continue

        # heurísticas para evitar secciones grandes
        vi = txt.count("Valor informativo")
        vd = txt.count("Ver detalle")
        if vi <= 2 and vd <= 2 and 150 < len(txt) < 1400:
            best = node
            best_text = txt
            break

        # fallback: si es enorme, no lo tomamos
        # pero seguimos subiendo por si encontramos algo más “card-like”

    if best:
        return best, best_text

    # fallback final: usar el padre inmediato del link
    parent = a_tag.parent
    if parent:
        return parent, parent.get_text(" ", strip=True)
    return None, a_tag.get_text(" ", strip=True)

def extract_bn_price(text: str) -> str:
    m = re.search(r"Valor\s+informativo:\s*(₡\s*[\d\.,]+|¢\s*[\d\.,]+|\$\s*[\d\.,]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else extract_price(text)

def extract_bn_province(text: str) -> str:
    # BN muestra ubicaciones tipo "CARTAGO, ALVARADO, CAPELLADES" -> tomamos solo la primera parte.
    m = re.search(r"\b([A-ZÁÉÍÓÚÑ ]+)\s*,", text)
    if m:
        prov = " ".join(m.group(1).split()).title()
        if prov.lower() == "san jose":
            return "San José"
        if prov.lower() == "limon":
            return "Limón"
        return prov
    return extract_province(text)

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

        container, snippet = bn_best_container(a)

        # título
        title = ""
        if container:
            h = container.find(["h1", "h2", "h3", "h4"])
            if h:
                title = h.get_text(" ", strip=True)
        if not title:
            title = a.get("title") or a.get_text(" ", strip=True) or snippet[:120]

        province = extract_bn_province(snippet)
        price = extract_bn_price(snippet)
        code = extract_code_generic(snippet) or extract_code_generic(full)

        stable = f"BN:{code}" if code else f"BNURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        items.append({
            "url": full,
            "title": title,
            "location": province,
            "size": extract_size(snippet),
            "price": price,
            "id": item_id
        })

    return items


# -------------------------
# BCR (se mantiene tu versión mejorada anterior si ya te funciona)
# -------------------------
def extract_bcr_price(text: str) -> str:
    m = re.search(r"Precio\s*:?\s*(¢\s*[\d\.,]+|₡\s*[\d\.,]+|\$\s*[\d\.,]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else extract_price(text)

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
    }
    for token in up:
        if token in mapping:
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
    code = extract_code_generic(text) or extract_code_generic(url)
    return code

def parse_bcr(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    seen_local = set()

    candidates = soup.find_all(lambda tag: tag.name in ["div", "section", "article", "li"] and "Precio" in tag.get_text(" ", strip=True))
    if not candidates:
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
            title = block_text[:120] if block_text else full

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
            "location": province,
            "size": extract_size(block_text),
            "price": price,
            "id": item_id
        })

    return items


# -------------------------
# BAC - FIX: ahora sí detectar propiedades y links "Ver más"
# -------------------------
def extract_bac_price(text: str) -> str:
    # BAC usa "$" y también "Precio con Descuento: $215,438" [2](https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas)
    m = re.search(r"Precio\s+con\s+Descuento:\s*(\$\s*[\d,\.]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"Precio:\s*(\$\s*[\d,\.]+)", text, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return extract_price(text)

def parse_bac(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items: List[Dict[str, str]] = []
    if not html:
        return items

    soup = BeautifulSoup(html, "html.parser")
    seen_local = set()

    # Encontrar nodos que contengan "Precio con Descuento" (señal fuerte de tarjeta BAC) [2](https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas)
    price_nodes = soup.find_all(string=re.compile(r"Precio\s+con\s+Descuento", re.IGNORECASE))

    # Si no aparece, intentamos con "Cotizar" o "Ver más"
    if not price_nodes:
        price_nodes = soup.find_all(string=re.compile(r"(Cotizar|Ver\s+más)", re.IGNORECASE))

    for node in price_nodes:
        # subir para encontrar contenedor card que incluya un link "Ver más"
        container = None
        cur = node.parent
        for _ in range(10):
            if not cur:
                break
            txt = cur.get_text(" ", strip=True)

            # Evitar contenedores demasiado grandes (paginación/footers)
            if 120 < len(txt) < 1800:
                # Debe tener un ancla "Ver más" o al menos algún link
                a_ver = cur.find("a", href=True, string=re.compile(r"Ver\s+más", re.IGNORECASE))
                a_any = cur.find("a", href=True)
                if a_ver or a_any:
                    container = cur
                    break
            cur = cur.parent

        if not container:
            continue

        snippet = container.get_text(" ", strip=True)
        province = extract_province(snippet)
        price = extract_bac_price(snippet)

        # Link: preferimos el de "Ver más"
        a = container.find("a", href=True, string=re.compile(r"Ver\s+más", re.IGNORECASE))
        if not a:
            a = container.find("a", href=True)
        if not a:
            continue

        href = a["href"].strip()
        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        full = normalize_url(full)

        # Título: usualmente "Casa en Heredia, ..." [2](https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas)
        # Capturamos la primera línea significativa tipo "Casa en ..."
        title = ""
        mtitle = re.search(r"\b(Casa|Apartamento|Terreno)\b.*?(?=(Precio|Cuota|Cotizar|Ver\s+más))", snippet, re.IGNORECASE)
        if mtitle:
            title = " ".join(mtitle.group(0).split())
        if not title:
            # fallback: primeras palabras
            title = snippet[:120]

        # ID estable: usa URL + precio + provincia como fallback (BAC a veces no expone código)
        stable = f"BACURL:{full}"
        item_id = make_id(stable)

        if item_id in seen_local:
            continue
        seen_local.add(item_id)

        items.append({
            "url": full,
            "title": title,
            "location": province,
            "size": extract_size(snippet),
            "price": price,
            "id": item_id
        })

    return items


# -------------------------
# Otros parsers (heurísticos)
# -------------------------
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
        if any(k in href.lower() for k in ["casas", "detalle", "propiedad", "ficha", ".aspx"]):
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
# Main
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
            s = items[0]
            print(f"  Ejemplo: title='{s.get('title')}' | price='{s.get('price')}' | prov='{s.get('location')}'", flush=True)

        for it in items:
            item_id = it.get("id") or make_id((it.get("url", "") + it.get("title", "")))
            if item_id in seen:
                continue
            new_items.append({"bank": name, **it, "id": item_id})
            new_seen.add(item_id)

    # Resumen por Telegram SOLO en ejecución manual
    if GITHUB_EVENT_NAME == "workflow_dispatch":
        resumen = "📊 *Resumen de scraping*\n\n" + "\n".join([f"- {k}: {v}" for k, v in counts.items()])
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, resumen)

    if not new_items:
        print("\nNo hay nuevos items.", flush=True)
    else:
        print(f"\nSe detectaron {len(new_items)} propiedades nuevas.", flush=True)

    # Limitar envíos para evitar 429
    if MAX_SEND > 0 and len(new_items) > MAX_SEND:
        print(f"Aplicando MAX_SEND={MAX_SEND}. Enviaré {MAX_SEND} de {len(new_items)}.", flush=True)
        new_items = new_items[:MAX_SEND]

    # Enviar notificaciones
    for it in new_items:
        msg = (
            "🚨 *Propiedad nueva detectada*\n\n"
            f"🏦 *Banco:* {it.get('bank')}\n"
            f"🗺️ *Provincia:* {it.get('location') or 'No disponible'}\n"
            f"📐 *Tamaño:* {it.get('size') or '_No disponible_'}\n"
            f"💰 *Precio:* {it.get('price') or '_No disponible_'}\n"
            f"🔗 {it.get('url')}\n"
        )
        print("Enviando:", it.get("url"), flush=True)
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
        time.sleep(SLEEP_BETWEEN_MSG)

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



