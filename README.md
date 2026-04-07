# PropAlertBot - Notificaciones de propiedades (Costa Rica)

Bot que detecta propiedades nuevas publicadas por bancos y envía notificaciones por Telegram.
Diseño: GitHub Actions + Python + Telegram (solución gratuita).

## Archivos incluidos
- `bot.py` : script principal (parsers adaptados a tus URLs).
- `requirements.txt`
- `state.json`
- `.github/workflows/scrape.yml`

## Variables / Secrets en GitHub
En el repositorio, añade en **Settings > Secrets and variables > Actions**:
- `TELEGRAM_BOT_TOKEN` = token del bot creado con @BotFather.
- `TELEGRAM_CHAT_ID` = id del chat o canal donde recibirás mensajes.

## Pasos rápidos
1. Crear repo en GitHub y subir los archivos.
2. Añadir los Secrets indicados.
3. (Opcional) Probar localmente:
   - `export TELEGRAM_BOT_TOKEN="..."`
   - `export TELEGRAM_CHAT_ID="..."`
   - `python -m pip install -r requirements.txt`
   - `python bot.py`
4. Ejecutar workflow manual desde Actions (Run workflow) o esperar al cron (cada 10 min).
5. Revisar logs en Actions y confirmar mensajes en Telegram.

## Notas importantes
- **Ajustes de parsers**: los parsers incluidos son heurísticos. Si alguna web carga contenido por JavaScript o cambia estructura, edita la función correspondiente (`parse_bn`, `parse_bcr`, etc.) usando selectores concretos.
- **Identificador único**: el script genera un `id` por cada enlace/título. Si un banco publica un folio/ID explícito, modifica el parser para usarlo (recomendado).
- **Intervalo**: cron cada 10 minutos por defecto; puedes cambiar a `*/5` si lo deseas, pero evita intervalos muy cortos para no ser bloqueado.
- **Respeto legal**: revisa términos de uso de cada sitio y `robots.txt`.
