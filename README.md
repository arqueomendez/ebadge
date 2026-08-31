# ebadge

Habla directo por BLE con un pin LCD BG02/BW03 (familia "DG01", app SuperBand/FitPro),
sin pasar por el celular. Es un port a Python (`bleak`, corre nativo en Windows) del
protocolo que documentó/reversó el proyecto
[DynamicDevices/lcd-badge-ble](https://github.com/DynamicDevices/lcd-badge-ble)
(Linux/BlueZ + Rust). Los offsets y constantes de `ebadge/protocol.py` están
copiados línea por línea de su código fuente (`dg01-ble/src/main.rs` y
`dg01-ble/src/dial_upload.rs`), no del resumen en prosa de `PROTOCOL.md`, así que
deberían coincidir con lo que hace su herramienta real.

Ver [`BITACORA.md`](BITACORA.md) para el historial completo de qué se probó,
qué falló y qué se descubrió sesión a sesión — este README solo describe el
estado *actual*.

## Antes de correrlo: dónde ejecutarlo

Este código vive en tu carpeta `G:\ebadge`, pero **para hablar por Bluetooth
tiene que correr en tu Python nativo de Windows**, no dentro de esta sesión de
Claude: el shell que uso para editar estos archivos es una VM Linux aislada sin
ningún adaptador Bluetooth (lo comprobé — no hay `/sys/class/bluetooth` ni
`bluetoothctl`). Lo que sí pude hacer aquí, y ya corrí, es la parte que no
necesita hardware: los tests en `tests/` (20/20 pasan) verifican que el
armado/parseo de frames 0xCD, y ahora también el contenedor de imagen RU50
(ver más abajo), coinciden byte a byte con el código de referencia.

Para correrlo de verdad, abre PowerShell o cmd **en tu máquina** (no en Claude) y:

```powershell
cd G:\ebadge
uv sync                          # instala bleak, pillow, etcpak, etc.
uv run python main.py scan       # lista dispositivos BLE cercanos, busca tu BG02/BW03
uv run python main.py dial-dims  <MAC>
uv run python main.py upload-dial <MAC> foto.jpg
```

(`<MAC>` es la dirección BLE que te muestre `scan`, tipo `AA:BB:CC:DD:EE:FF`; si
no aparece por nombre, `scan` igual la lista.)

## El hallazgo del 2026-08-30: el badge no quiere RGB565, quiere "RU50"

Todas las subidas reales llegaban a los 576/576 chunks (115200 bytes = un
frame RGB565 de 240x240) y el paso `finish` se rechazaba al instante con
`status=1` ("check failed") sin importar si el checksum final se mandaba en
4 u 8 bytes. Eso apuntaba a que el problema no era el *formato del checksum*
sino el *contenido* que se estaba subiendo.

Buscando de nuevo con las pistas de hardware que arrojó `device-info`
(placa `LJ733_MB_V1.1`, firmware `V35509`) apareció un commit de mayo 2026 en
`dg01-ble` probado contra exactamente el mismo modelo de placa ("LJ733"), que
dice textual: *"Upload reaches 99% successfully. Remaining blocker is RU50
proprietary image format."* Ese mismo proyecto trae una herramienta
(`ru50_convert.py`) que documenta, a partir de ingeniería inversa del
conversor nativo del fabricante (JieLi `BmpConvert 1.6.0`), que la app real
NO manda RGB565 crudo: manda un contenedor propietario "RU50" con un header
fijo de 1104 bytes más una textura comprimida **ETC2** (no píxeles crudos),
más dos checksums CRC16 (con una tabla propia del fabricante, no un CRC
estándar) — ver `ebadge/ru50.py` para el detalle byte a byte y por qué una
parte del script original (el orden en que rellena la zona "reservada" del
header) estaba pisando sus propios campos, y cómo se corrigió acá.

`upload-dial` ahora usa este formato por defecto (`--format ru50`, requiere
el paquete `etcpak`, que ya viene en las dependencias). Para 240x240 el blob
resultante pesa ~29.9 KB en vez de 112.5 KB — 150 chunks en vez de 576.

**Corrección del 2026-08-31 (mediodía):** al principio se describió esto
como "una hipótesis con buena evidencia". Revisando la historia completa
de git de `dg01-ble`, la evidencia real resultó ser más débil de lo que
parecía: `ru50_convert.py` nació 3 días después de que el issue #2 tratara
RU50 como una incógnita total, con ayuda de un agente de IA, decompilando
una librería (`libjl_bmp_convert.so`) que nunca se subió al repo, y con un
bug de escritura del header presente sin cambios desde su primer commit —
fuerte indicio de que nadie, ni su propio autor, llegó a probarlo contra
un badge real. Se probaron dos hipótesis sobre el contenido (relleno a un
largo fijo de bytes, y poner en cero los 6 campos "de significado
desconocido") y ambas fallaron igual: silencio total en el `finish`.

**Corrección del 2026-08-31 (tarde) — el fix real:** en vez de seguir
adivinando, se bajó el AAR oficial de JieLi
(`Jieli-Tech/Android-JL_Bluetooth`, `libs/BmpConvert_V1.6.0_10605-release.aar`,
que sí es un zip real y verificable) y se desensambló directamente
(`objdump`/`readelf`) la función real `br35_bmp_to_res` de
`libjl_bmp_convert.so` — el mismo camino que la línea de chips "707N"
(`TYPE_707N_*`) usa para producir blobs RU50. Comparando esa desensamblada
contra `ebadge/ru50.py` byte a byte aparecieron dos errores concretos,
ninguno de los cuales tenía que ver con los 6 campos que se venían
sospechando:

1. Una de las constantes del header (`HDR_QW_04`) tenía dos mitades de 16
   bits invertidas respecto del valor real.
2. **El orden de los campos desde el offset 0x3C estaba mal.** El real es:
   `crc_header` (u16) en 0x3C, `crc_payload` (u16) en 0x3E, flags (u32) en
   0x40, ancho en 0x44, alto en 0x46, **el largo real del payload ETC2**
   en 0x48, y recién en 0x4C una constante fija de valor 0x450 (el tamaño
   del propio header) — **no el largo del payload otra vez**, como
   asumíamos. Si el firmware lee el largo del payload en la posición que
   nosotros tomamos por "constante fija" (y viceversa), tiene sentido que
   se quede esperando una cantidad de datos completamente distinta a la
   que mandamos — que es exactamente el síntoma que veníamos observando
   (todos los chunks confirmados, silencio total e indefinido en el
   `finish`, nunca un rechazo explícito).

También se corrigió el rango de la zona "reservada" (es `[0x50, 0x450)`,
no `[0x14, 0x414)` como se asumía) y la construcción de los 18 bytes que
alimentan el segundo CRC16 (`crc_header`), que ahora usan los valores
reales (`crc_payload`, flags, ancho, alto, largo del payload, y la
constante 0x450) en vez de un relleno en cero. El detalle completo,
byte por byte, está en `ebadge/ru50.py` y en `BITACORA.md`.

Esta vez la evidencia es de otro calibre: no es una reconstrucción de IA
sin verificar, es la lectura directa del código real que el propio
fabricante distribuye y que corre en los teléfonos.

**Resultado contra hardware real: sin cambios.** Una subida real con este
header corregido dio el mismo silencio total, byte por byte idéntico a
todas las variantes anteriores. Cuatro hipótesis de contenido distintas
(header original, relleno a tamaño fijo, campos "desconocidos" en cero,
header corregido por desensamblado) dieron el mismo resultado exacto --
señal de que el problema probablemente no estaba en el contenido del
archivo.

**Corrección del 2026-08-31 (noche) — encontramos por qué "nada cambiaba":**
releyendo con más cuidado la documentación de `dg01-ble` (no solo los
fragmentos ya citados) apareció un formato de notificación completo que
este proyecto nunca implementó: frames cortos de 8 bytes que empiezan con
**`0xDC`** en vez de `0xCD` -- un camino de ack totalmente separado que el
propio `dg01-ble` documenta como válido para el `finish` (`cmd 31 sub 3`)
y para el `start` (`cmd 31 sub 2`), confirmado contra hardware real en el
mismo commit de mayo (`096490d`). Nuestro `CdNotifyAssembler` sólo
reconocía frames `0xCD` -- cualquier notificación `0xDC` se descartaba en
silencio, sin dejar rastro en el log. Si el ack real de `finish` de este
firmware llega en formato `0xDC`, tiene sentido que cambiar el contenido
del archivo nunca haya cambiado nada: el problema nunca fue el contenido,
sino que no podíamos ver la confirmación de éxito. Corregido en
`ebadge/protocol.py`/`ebadge/ble.py` -- detalle completo en `BITACORA.md`.
Pendiente confirmar contra hardware real.

`--format rgb565` se mantiene como comparación/respaldo, pero cada subida
real que hicimos con él fue rechazada en el `finish` (de forma explícita e
instantánea, a diferencia del silencio que daba RU50).

## Qué está verificado vs. qué es "lo más plausible que tenemos"

Verificado byte a byte contra el código fuente de dg01-ble (con tests que lo prueban):

- El framing `0xCD` (marcador, largo, cmd, sub-key, largo de payload).
- El request de `dial-dims` (cmd 32 / sub 2, sin payload).
- El parseo de la respuesta de `dial-dims` (screen_type, grade, width, height, y el
  byte de config opcional).
- Los tres pasos de subida (`start` cmd31/sub2 de 17 bytes, `chunk` cmd31/sub1 con
  checksum de 16 bits, `finish` cmd31/sub3 con la suma de 32 bits de todo el
  archivo — **no** un largo+checksum como sugería el resumen de `PROTOCOL.md`;
  el código fuente real, y un fix de mayo 2026 probado contra nuestra misma
  placa "LJ733", confirman que solo manda la suma de 4 bytes. `--finish-length-prefix`
  deja el viejo formato de 8 bytes disponible solo para comparar).
- El reensamblado de notificaciones fragmentadas (`CdNotifyAssembler`).
- El parseo de los ACK (`parse_dial_watch_ack_status` / `parse_cd_notify_status`),
  incluyendo que el ACK de `finish` se lee distinto (parser "loose") que los de
  `start`/`chunk`.
- El envío fragmentado en trozos de 20 bytes con 3ms de espera entre trozos
  (`gatt_write_fragmented`), igual que hace la app FitPro según su propio código.

Sin verificar contra hardware real (nadie puede saberlo sin probar con tu BG02):

- **Qué UUID de servicio usa tu unidad exacta.** `dg01-ble` distingue hardware
  DG01 real (`7e40...`) de lo que habla la app vía `--apk-uart` (`6e40...`).
  `ebadge/ble.py` prueba ambos automáticamente al conectar.
- **Los campos "OEM" del payload de `start`** (`font_position`, `custom`, los 4
  bytes `mid4`, y el RGB): son campos que la app usa pero cuyo significado no
  está documentado. Uso los mismos defaults que trae la CLI de dg01-ble
  (`mid4=15a20008`, rgb=255/255/255, custom=true) por ser lo más parecido a un
  valor "conocido-funcional" que hay, pero si tu badge rechaza el `start` (ver
  "Debuggeando" abajo), puede que necesite otros valores — quizá derivados del
  ancho/alto en vez del hex fijo (dg01-ble tiene una opción para eso que no
  alcancé a reversar).
- Si tu BG02 en particular es realmente idéntico en firmware al DG01 que
  documentó DynamicDevices (mismo fabricante base, pero "parecen ser el mismo"
  no es "son el mismo").

## Sobre los códigos de status "conocidos" (1/3/4/5/7)

`ebadge/protocol.py` trae una tabla (`STATUS_ERRORS`) con nombres tipo
"low battery" o "charging" para algunos códigos, heredada de cómo
`dg01-ble`/`PROTOCOL.md` leen las constantes del APK decompilado. **No
confíes en esos nombres para tu badge**: `status=3` ("low battery") ya
salió una vez con la batería al 90% confirmada, y `status=4` ("charging")
salió cinco veces seguidas sin ningún cable cerca del badge (ver
`BITACORA.md`, 2026-08-31). Se probó por un rato hacer que `upload-dial`
abortara de inmediato al ver estos códigos -- se revirtió el mismo día
porque esa suposición resultó falsa. Si ves uno de estos códigos en el log,
tratalo como "el firmware no dio el ack que esperábamos", no como un
diagnóstico confiable de la causa.

Si un intento completo de subida falla y se reintenta, el programa ahora
espera `--upload-attempt-delay` segundos (15 por defecto, antes 2) antes de
reconectar -- por si el badge sigue ocupado terminando de procesar/escribir
el intento anterior.

## Debuggeando si algo no calza

Corre cualquier comando con `--debug` para ver en vivo cada frame crudo
enviado/recibido en hex:

```powershell
uv run python main.py --debug dial-dims <MAC>
```

Si `dial-dims` no reconoce la respuesta, o `upload-dial` se cae en el ACK de
`start`, el log de `--debug` te va a mostrar los bytes exactos que mandó el
badge — con eso puedo ajustar el parser sin adivinar. Guardar un capture así
(o, mejor, uno hecho con un sniffer BLE tipo nRF52840 mientras usas la app
SuperBand real) es el único modo de cerrar los puntos de la lista anterior con
certeza en vez de "lo más plausible".

## Estructura

- `ebadge/protocol.py` — framing 0xCD, comandos, parseo de respuestas (sin BLE).
- `ebadge/image.py` — conversión de cualquier imagen a RGB565 little-endian, y el
  redimensionado/recorte (`fit_image`) que comparten los dos formatos de salida.
- `ebadge/ru50.py` — el contenedor de imagen "RU50" (header + textura ETC2 +
  CRC16 propio) que el badge probablemente espera en vez de RGB565 crudo.
- `ebadge/ble.py` — cliente `bleak` (conexión, reensamblado, subida por chunks).
- `ebadge/cli.py` — CLI (`scan`, `dial-dims`, `upload-dial`, `find`, `services`, `device-info`).
- `tests/` — tests que corren sin hardware (`uv run pytest`).

## Familia de pines táctiles a color (Monokaro/Beambox/AniPin)

Esto no sirve para esos: no encontramos ningún proyecto de reversa para esa
familia. Sería reversar desde cero (capturar tráfico con un sniffer BLE o un
MITM en el teléfono) si en algún momento trabajas con uno de esos en vez del
BG02.
