# Monitor de PS5

Gemelo del monitor de teles. Corre en GitHub Actions cada hora (minuto 47) y avisa por Telegram (mismo bot) cuando
una PS5 Standard, Digital o Pro vuelve a tener stock o baja de precio, con la mejor opción de cuotas.

- Corridas y botón "Run workflow": https://github.com/nachomit02/monitor-ps5/actions
- Página: https://nachomit02.github.io/monitor-ps5/

## Dónde busca

- Catálogo VTEX (precio, stock real simulando la compra y cuotas por tarjeta): Frávega, Carrefour, Jumbo, Disco,
  Vea, Coppel, On City, Cetrogar, Naldo, Castillo y Provincia Compras.
- Mercado Libre por API (permiso de app, no el de usuario: no choca con el monitor de teles). Sin cuotas: la API no las da.
- Con navegador: Megatone y Casa del Audio.

Probadas y descartadas (nov 2026): Garbarino (cerró), Musimundo (en mantenimiento), Coto, Fullh4rd, Venex, Mexx,
Maximus, Hiper Libertad (no venden consolas o bloquean). Para sumar una tienda VTEX, agregala a `vtex` en `config.json`.

## Cómo decide qué modelo es

No hay un código único como en las teles: `clasificar()` en `monitor.py` lee el nombre de la publicación.
Pro si dice "Pro" o CFI-7xxx; Digital si dice "Digital"; si no, Standard. Descarta joysticks, juegos, covers,
lectoras, Portal, VR, reacondicionados y todo lo que cueste menos del 40% del tope.
Los bundles con juegos cuentan como el modelo de la consola.

## Cuotas

Muestra la mayor cantidad de cuotas sin interés (y con qué tarjetas). Si la tienda no tiene sin interés,
muestra el plan más largo con su total. Algunos vendedores del marketplace de Frávega cargan el precio sin IVA:
el monitor usa el total en 1 pago, que sí lo incluye.

## Comandos en tu PC

En PowerShell, en esta carpeta:

- `..\monitor_teles\.venv\Scripts\python.exe monitor.py --dry-run` → revisa sin avisar (deja `state_prueba.json`)
- `..\monitor_teles\.venv\Scripts\python.exe nube.py actualizar` → sube cambios de `config.json` o del código
- Topes de precio: `precio_max` de cada producto en `config.json`
