# Bitácora del proyecto

Registro cronológico de qué se probó, qué falló, qué se descubrió y qué
falta. El `README.md` describe el estado *actual* del proyecto (qué está
verificado, cómo correrlo); este archivo es el historial de cómo se llegó
ahí, para no repetir experimentos ni perder el hilo entre sesiones.

Convención: una entrada nueva por sesión de trabajo o hallazgo relevante,
fecha en `YYYY-MM-DD`, más reciente al final.

---

## 2026-08-30 — Arranque: puerto del protocolo a Python/bleak

**Objetivo.** Hablar directo por BLE con el ebadge BG02/BW03 (pin con
pantalla LCD circular) desde Windows, sin depender de la app SuperBand,
subiendo imágenes reversando el protocolo. Punto de partida: el proyecto
[DynamicDevices/lcd-badge-ble](https://github.com/DynamicDevices/lcd-badge-ble)
(Rust/BlueZ), que documenta esta familia de badges ("DG01-style").

**Hecho:**
- Port línea por línea de `dg01-ble/src/main.rs` y `dial_upload.rs` a
  `ebadge/protocol.py` (framing 0xCD, comandos, parseo de acks).
- Cliente BLE nativo Windows con `bleak` (`ebadge/ble.py`) — auto-detección
  de UUID de servicio `7e40...` (hardware real) vs `6e40...` (app).
- CLI (`ebadge/cli.py`): `scan`, `dial-dims`, `upload-dial`, `find`.
- Confirmado contra hardware real: la placa es 240x240 (no 360x360 como el
  DG01 de referencia), UUID `7e40...`, `dial-dims` funciona y devuelve bytes
  correctos.

**Bugs reales encontrados y corregidos, todos vía log `--debug` contra el
BW03 físico del usuario:**
1. `--debug` solo funcionaba después del subcomando (parser mal armado) →
   parser padre compartido con `parents=[common]`.
2. Numeración de ack de chunk mal asumida como `1000+seq`; el valor real es
   `1000 + chunks_recibidos_hasta_ahora` (o sea `1000+seq+1`).
3. `_await_ack` tenía `want_status`/`want_sub` declarados pero nunca
   usados para filtrar — aceptaba el primer frame reconocible aunque no
   fuera el ack esperado.
4. Fragmentación manual de 20 bytes por escritura BLE resultaba inestable
   con este badge (MTU negociado 517); `--fragment-size 0` (una sola
   escritura por frame) mejoró mucho la fiabilidad, aunque no la eliminó
   del todo.
5. `OSError` crudo de WinRT (`start_notify` fallando con
   `El usuario ha cancelado la operación`, MTU cayendo a 23) no lo atrapaba
   el retry loop porque solo esperaba `ProtocolError` → se amplió a
   `(ProtocolError, OSError, RuntimeError, TimeoutError)`.
6. Payload de `finish` (cmd31/sub3): dg01-ble original manda 4 bytes (solo
   la suma); se cambió a 8 bytes (largo+suma) siguiendo el resumen de
   `PROTOCOL.md` sobre el APK decompilado — **ambas versiones fueron
   rechazadas idénticamente con `status=1` ("check failed")**, incluso en
   una subida con los 576/576 chunks acomplidos con éxito. Quedó como el
   problema abierto más importante del día.

**Otros hallazgos:**
- No existe ningún comando de borrado/galería en el protocolo reversado
  (revisado el código fuente completo de dg01-ble: sin `delete`/`clear`/
  `list`/`gallery`). Se agregó `--solid R,G,B` como forma de "limpiar" la
  pantalla mientras tanto.
- Se agregaron comandos de diagnóstico de solo lectura (`services`,
  `device-info`) usando servicios BLE estándar (Device Information 0x180A,
  Battery 0x180F) — sin tocar el protocolo propietario. Esto reveló:
  `Manufacturer=LJ733_V1_BadgeOK`, `Hardware=LJ733_MB_V1.1`,
  `Firmware=V35509`, batería real 90%.
- Batería baja (`status=3`) fue causa real de al menos un fallo, pero no
  explica todos los cortes intermitentes a mitad de subida (chunk 38, 97,
  236, 351, 409 en distintas corridas) — sigue sin causa raíz confirmada;
  sospecha actual: stack BLE de Windows bajo tráfico sostenido, o
  interferencia RF de otros dispositivos BLE cercanos.
- Evaluación de acceso directo al firmware (OTA/DFU, SWD/JTAG, herramientas
  del fabricante): se descartó por alto riesgo/baja probabilidad de éxito
  contra un producto comercial probablemente bloqueado; alternativa
  recomendada si hiciera falta un capture real: log HCI snoop de Android
  (BT snoop) grabando la app SuperBand real.

---

## 2026-08-31 (hoy) — El hallazgo RU50: el badge no quiere RGB565

**Disparador.** Con los datos de hardware nuevos (`LJ733_MB_V1.1`,
`V35509`) se volvió a buscar información online.

**Encontrado:**
- Un commit de mayo 2026 en `dg01-ble` (`096490d`, PR #3 de un colaborador
  externo `jackghx`) probado contra **exactamente nuestra misma placa**
  ("model LJ733"). Ese commit:
  - Confirma con una captura real (iOS sysdiagnose) que el payload de
    `finish` correcto es **4 bytes (solo suma), no 8** — el `PROTOCOL.md`
    que seguimos el día anterior estaba desactualizado en ese punto.
  - Dice textual: *"Upload reaches 99% successfully. Remaining blocker is
    RU50 proprietary image format."*
- Una herramienta del mismo repo (`ru50_convert.py`), que documenta —a
  partir de ingeniería inversa del conversor nativo del fabricante (JieLi
  `BmpConvert 1.6.0`)— que la app real NO manda RGB565 crudo: manda un
  contenedor propietario **"RU50"**: header fijo de 1104 bytes + textura
  comprimida **ETC2** (no píxeles crudos) + dos checksums **CRC16** con
  tabla propia del fabricante.
- Un issue abierto en el repo (#2, "blocked on image format (RU50)") que
  confirma que esto sigue sin resolverse río arriba tampoco — nadie ha
  reportado ver una imagen custom real en la pantalla de uno de estos
  badges todavía.

Esto explica por qué el `finish` se rechazaba siempre con `status=1` sin
importar el formato del checksum: no era un problema de *cómo* se
empaquetaba la suma, sino de que el *contenido* mandado (RGB565 crudo) no
tiene la forma que el firmware espera.

**Implementado:**
- `ebadge/ru50.py` (nuevo): encoder completo del contenedor RU50 — header,
  compresión ETC2 vía `etcpak`, las dos CRC16 con la tabla del fabricante
  (tabla de 512 bytes embebida). Se detectó y corrigió un bug real en el
  script de referencia: escribía los campos del header (ancho/alto/largo/
  CRC) y *después* rellenaba con ceros la zona "reservada" que los
  superpone — pisándolos. Se reordenó para que sobrevivan.
- `ebadge/protocol.py`: `dial_finish_payload` vuelve a 4 bytes por defecto
  (`include_length=True` como escape hatch para el viejo formato de 8).
- `ebadge/cli.py`: `upload-dial --format {ru50,rgb565}` (default `ru50`),
  `--finish-length-prefix`, chequeo temprano de que `etcpak` esté instalado
  (para no reintentar 3 veces a ciegas si falta).
- `pyproject.toml`: agregado `etcpak` (confirmado wheel para Windows).
- `tests/test_ru50.py` (nuevo, 6 tests): layout de bytes, CRC16, y un
  round-trip de compresión real verificando el tamaño esperado.
- `README.md`: nueva sección explicando el hallazgo, lista de verificado
  actualizada.

**Verificado (sin hardware, 19/19 tests):** para 240x240, el blob RU50 pesa
29,904 bytes (150 chunks de 200B) contra 115,200 bytes (576 chunks) del
RGB565 crudo — la razón 4:1 coincide exactamente con lo esperado de ETC2 a
4 bits/píxel.

**Sin verificar todavía (hace falta hardware real):** si el badge acepta
este formato. Es la hipótesis con mejor evidencia que tenemos, no una
solución confirmada — igual que le pasó al fix de mayo, que solo llegó a
"99%" antes de toparse con este mismo bloqueo.

**Pendiente ahora mismo:** corrida real con
`uv run python main.py --debug upload-dial BB:50:43:DE:XX:XX imagen\reunion.jpg`
— resultado todavía no reportado al momento de escribir esto.

---

## 2026-08-31 (misma sesión) — Primera corrida real con RU50: dos hallazgos separados

Corrida real: `upload-dial BB:50:43:DE:XX:XX imagen\reunion.jpg`, formato
`ru50` (default nuevo). Dos cosas distintas pasaron, una en cada intento:

**Intento 1/3 — el `finish` ya no se rechaza al instante.** Los 150/150
chunks (bien: era el tamaño esperado del blob RU50, 29904 bytes) se
mandaron y confirmaron sin problema. En el paso `finish`:
- Un `status=1` que el log muestra como "check failed" **no era en
  realidad una respuesta al `finish`**: decodificando el frame
  (`cd0009200101000400000001`) es cmd32/sub1 -- un ACK de *chunk*, no de
  finish. Fue casi seguro el eco tardío/duplicado del ack del último chunk
  (el mismo patrón de "status=1 espurio" que ya se había visto en el chunk
  0 el 2026-08-30), capturado por el parser "loose" del finish porque ese
  parser no filtra por cmd/sub.
- Aparte de eso, silencio total durante 45s (dos intentos), sin ningún
  `status=2` real ni un rechazo explícito de finish. Antes (con RGB565
  crudo) el rechazo era instantáneo y repetible. Ahora es silencio. Es una
  señal ambigua pero más alentadora que antes: podría ser que el firmware
  esté tardando en procesar/validar un contenido que por fin tiene una
  forma plausible (decodificar ETC2, verificar CRC16, escribir a flash), o
  podría seguir siendo datos mal formados que el firmware simplemente
  descarta sin avisar. Sigue sin confirmarse cuál de las dos es.

**Intentos 2/3 y 3/3 — bloqueados por un problema totalmente distinto:**
apenas se reconecta y se manda el `start`, el badge contesta `status=4`
("charging" -- rechazo explícito de firmware, "se niega a actualizar el
watchface mientras está cargando") cinco veces seguidas. Esto **no es un
bug del retry**: el retry hizo exactamente lo que tenía que hacer
(reconectar y reintentar 3 veces), pero un reintento no puede arreglar que
el badge crea que está enchufado a un cargador -- va a repetir el mismo
rechazo indefinidamente hasta que cambie esa condición física.

**Corregido:** hasta ahora, cualquier status que no fuera el esperado se
trataba igual (advertencia + seguir leyendo), incluyendo códigos de rechazo
inequívocos del firmware -- así que el loop de reintento completo (3
intentos) se gastó entero reconectando contra un rechazo que nunca iba a
cambiar. Se agregó `protocol.FATAL_STATUS_CODES` (`3` batería baja, `4`
cargando, `5` sin memoria, `7` no listo -- **no** incluye `1`, porque ese sí
se vio ser ruido espurio en una captura real) y `ble.DeviceRefused`: ahora
`_await_ack` corta al instante ante uno de estos códigos, `_send_with_retry`
no lo reintenta, y `upload-dial` para toda la operación con un mensaje
explícito en vez de gastar los 3 intentos completos. Agregado
`tests/test_ble_fatal_status.py` (3 tests, sin hardware) verificando esto.

**Pendiente:** repetir la corrida con el badge físicamente desconectado de
cualquier cable/base de carga, para aislar el resultado real del `finish`
con RU50 sin el ruido del `status=4`.


## 2026-08-31 (misma sesión) — Revertido el fast-fail: la etiqueta "charging" era falsa

El usuario confirmó que **no había ningún cable conectado** durante la
corrida de arriba. Eso tira abajo directamente la interpretación literal de
`status=4` como "está cargando" -- y no es la primera vez que una de estas
etiquetas falla: `status=3` ("low battery") ya se había visto una vez antes
con la batería confirmada al 90% por `device-info` en la misma sesión del
2026-08-30.

Dos contradicciones independientes es señal suficiente de que la tabla
`STATUS_ERRORS` (nombres heredados de cómo `dg01-ble`/`PROTOCOL.md` leen las
constantes del APK decompilado -- `ERROR_BATTERY_LOW`, `ERROR_CHARGE_BATTERY`,
etc.) no es confiable para *esta* placa/firmware ("LJ733"/V35509). Puede ser
un firmware bifurcado que reusa esos mismos códigos numéricos para otra cosa
(por ejemplo "ocupado" en vez de "cargando"), o directamente otra tabla.

**Revertido:** el fast-fail agregado hacía 20 minutos (`FATAL_STATUS_CODES`,
`ble.DeviceRefused`, abortar toda la operación al ver 3/4/5/7) se sacó
completo -- estaba construido sobre una suposición que la evidencia de
hardware acababa de desmentir, y abortar sin reintentar es peor que
reintentar de más si la suposición está mal. `_await_ack` volvió a su
comportamiento tolerante original (loggear y seguir leyendo ante cualquier
status que no sea el esperado, sea cual sea). Se borró
`tests/test_ble_fatal_status.py` (probaba el comportamiento revertido).

**Lo que sí se mantuvo, porque no depende de la etiqueta siendo correcta:**
la pausa entre reintentos completos de subida subió de 2s (fijo) a
`--upload-attempt-delay` (default 15s) -- si lo que sea que causó ese status
es que el badge sigue ocupado terminando el intento anterior (encaja con
que el `finish` con RU50 haya quedado en silencio 45s en vez de rechazar al
instante, ver la entrada anterior), reconectar 2 segundos después nunca le
iba a dar tiempo a terminar.

**Aprendizaje para el resto del proyecto:** cualquier nombre en
`STATUS_ERRORS` es una pista para el log, no un diagnóstico -- ya lleva 2/2
contradicciones reales. No tomar decisiones de "abortar" ni de UX basadas en
esos nombres sin una confirmación de hardware propia.

**Pendiente:** repetir la corrida real (misma imagen, mismo comando) con el
nuevo `--upload-attempt-delay 15` por defecto, y de paso confirmar si el
segundo/tercer intento completo dejan de toparse con ese status apenas se
les da más tiempo.


## 2026-08-31 (misma sesión) — `--upload-attempts` pasa a valer 1 por defecto

Pedido explícito del usuario tras ver el log de arriba: prefiere que
`upload-dial` falle de una sola vez cuando algo no sale bien, para poder
mirar qué pasó, en vez de que el programa reintente 3 veces solo por
reconectar y repetir el mismo fallo -- que es exactamente lo que pasó en la
corrida de más arriba (intentos 2 y 3 no aportaron información nueva, solo
tiempo).

**Cambiado:** default de `--upload-attempts` de `3` a `1`. Quien quiera el
reintento automático lo sigue teniendo disponible pasando el flag a mano
(`--upload-attempts 3`, por ejemplo), junto con `--upload-attempt-delay`
para la pausa entre intentos.


## 2026-08-31 (misma sesión) — Segunda corrida limpia (1 solo intento): mismo patrón, confirma reproducibilidad

Con `--upload-attempts 1` el log queda limpio de una corrida sola. Resultado
igual al de la corrida anterior en el paso `finish` (con la falsa alarma de
"charging" ya fuera de la ecuación):

- Los 150/150 chunks del blob RU50 se confirman sin problema (un
  `status=0` transitorio en el chunk 0, tolerado y resuelto solo, igual que
  el `status=1` espurio visto el 2026-08-30).
- Al mandar `finish`: un `status=1` que, decodificado, es cmd32/sub1 -- otra
  vez el eco tardío del ack del último chunk, no una respuesta real al
  finish.
- Después de eso: **silencio total** durante 90s (2 intentos de 45s cada
  uno), interrumpido solo por el mismo frame heartbeat/banner sin relación
  (`cmd21/sub12`, payload todo ceros) que ya se veía en corridas anteriores.
  Nunca llega ni `status=2` (éxito) ni un rechazo explícito de finish.

Dos corridas independientes con el mismo resultado exacto en el paso finish
-- ya no es ruido de una sola vez, es el comportamiento real y reproducible
del formato RU50 contra este firmware.

**Dos hipótesis en pie, sin forma de distinguirlas solo con el log de BLE:**
1. El badge está genuinamente ocupado escribiendo/verificando la textura
   (descomprimir ETC2, verificar los CRC16, escribir a flash) y 45-90s no
   alcanza -- necesitaría un timeout bastante más largo para confirmarlo.
2. El badge se cuelga/queda trabado procesando datos que pasan el chequeo
   inicial (por algo se movió de "rechazo instantáneo" con RGB565 a
   "silencio" con RU50) pero tienen algún campo del header o el CRC mal, y
   nunca termina ni contesta -- necesitaría un power-cycle para recuperarse.

**Pendiente de evaluar (requiere al usuario, no más código a ciegas):**
- Mirar la pantalla física del badge inmediatamente después de un intento
  así: ¿cambió algo, se puso en blanco, mostró algo corrupto, o sigue
  exactamente igual que antes de subir?
- Repetir con un `--finish-timeout` mucho más largo (ej. 180-300s) para ver
  si *alguna vez* contesta, distinguiendo "lento" de "trabado".
- Después de un intento fallido así (sin apagar/reconectar el badge),
  probar `dial-dims` o `device-info` de nuevo: si el badge sigue
  respondiendo normal a esos comandos, está vivo/ocupado, no colgado.


## 2026-08-31 (misma sesión) — El badge está vivo y no se corrompe: solo descarta el intento

Dos datos nuevos del usuario después del intento fallido, sin reiniciar el
badge:

1. **La pantalla no queda corrupta ni en blanco** -- vuelve a mostrar la
   imagen más reciente (la que ya tenía antes de este intento). No hay
   ningún indicio visual de un estado a medio escribir.
2. `dial-dims` contestó normal (240x240, igual que siempre) justo después
   del fallo, sin reconectar en frío ni nada especial.

Esto descarta bastante claro la hipótesis de "firmware trabado/colgado
procesando datos corruptos": un firmware colgado no contestaría normal a un
comando no relacionado un segundo después. El badge está vivo, funcional, y
simplemente **descarta la transferencia entera y vuelve a lo que tenía**
cuando el `finish` no se completa -- lo cual también es tranquilizador para
seguir probando: no hay riesgo aparente de dejar el badge en un estado roto
con estos intentos.

Sigue sin poder distinguirse si esto pasa porque:
(a) el badge de verdad seguía "pensando" durante los 90s y recién al cortar
la conexión (que es lo que hace nuestro script al terminar/rendirse)
aborta y revierte como parte de la limpieza de la desconexión -- en cuyo
caso un timeout mucho más largo *mientras seguimos conectados* SÍ podría
llegar a ver un `status=2`; o
(b) el badge decide descartar la subida mucho antes (quizás casi de
inmediato) por algún campo del header/CRC RU50 que sigue mal, y solo
"parece" silencio porque no manda ningún aviso explícito de ese descarte,
revirtiendo recién al cerrar la conexión de cualquier forma.

**Próximo experimento sugerido:** repetir con `--finish-timeout` mucho más
alto (varios minutos) y `--finish-retries 1`, para ver si alguna vez llega
un `status=2` real estando todavía conectados -- eso separaría (a) de (b).


## 2026-08-31 (misma sesión) — 5 minutos de silencio total: no es lentitud, es otra cosa

Corrida con `--finish-timeout 300 --finish-retries 1`: cinco minutos
conectado, sin un solo `status=2`, sin ningún rechazo explícito -- nada
salvo el heartbeat de siempre cada ~30s. Esto descarta la hipótesis (a) de
la entrada anterior ("el badge está ocupado escribiendo/verificando, solo
necesita más tiempo"): ningún firmware real tarda 5+ minutos en procesar
30KB. El silencio es indefinido, no "lento".

**Nueva hipótesis, la más fuerte hasta ahora:** el firmware podría no
confiar en el campo `file_len` que declaramos nosotros mismos en el
`start` (cmd31/sub2) -- podría en cambio esperar, de forma fija, exactamente
`width*height*2` bytes (el tamaño de un frame RGB565 crudo para la
resolución que ya conoce de su propia pantalla: **115200 bytes** para nuestra
240x240) antes de considerar la transferencia completa, sin importar lo
que hayamos declarado. Eso explicaría *todo* lo visto hasta ahora de forma
coherente:

- **RGB565 (115200 bytes, calza exacto)**: el contador interno de bytes
  recibidos llega al total fijo que el firmware espera justo con el último
  chunk -- ahí sí evalúa el `finish` y su checksum, y lo rechaza rápido y
  explícito (`status=1`). O sea: SÍ llega a la etapa de validación.
- **RU50 (29904 bytes, muy por debajo de 115200)**: el contador interno
  nunca llega al total fijo esperado -- desde el punto de vista del
  firmware, la transferencia sigue "a medias" aunque nosotros ya mandamos
  `finish`. Nunca evalúa nada porque, para él, todavía faltan ~85KB de
  datos que nunca van a llegar. De ahí el silencio indefinido.

Si esto es correcto, el problema no es tanto "qué formato de imagen quiere
el badge" sino que el protocolo de transferencia cmd31 en sí mismo espera un
conteo de bytes fijo derivado de la resolución de pantalla, sin importar el
contenido -- lo cual sugeriría que **RGB565 sí podría ser el contenedor
correcto para este comando en particular**, y que el problema real sigue
siendo pegarle al checksum/algoritmo exacto que el firmware calcula, no el
formato de píxel.

**Prueba barata para confirmar/descartar esto sin más teoría:** rellenar el
blob RU50 con bytes de relleno (cero) al final hasta completar exactamente
115200 bytes totales enviados (sin tocar el header/CRC del contenido RU50
real, que sigue siendo válido en los primeros ~29904 bytes) y ver si el
`finish` deja de quedarse en silencio -- aunque sea para rechazarlo
explícitamente. Si el silencio se rompe, confirma la hipótesis del conteo
fijo de bytes. Implementado como `--pad-to-screen-bytes` en `upload-dial`
(ver commit/edición correspondiente) puramente como herramienta de
diagnóstico, no como solución.


## 2026-08-31 (misma sesión) — Prueba de `--pad-to-screen-bytes`: inconclusa, se cortó antes de llegar al finish

Con el relleno a 115200 bytes, la subida ahora manda 576 chunks (como
RGB565) en vez de 150. Se cortó en el **chunk 2/576**, sin ningún ack, tras
3 reintentos de 5s cada uno -- muy antes de llegar al paso `finish`, así que
esta corrida **no prueba ni descarta la hipótesis del conteo fijo de
bytes**, todavía queda pendiente.

Esto es casi con certeza el mismo problema de estabilidad de chunks ya
documentado el 2026-08-30 (cortes intermitentes en puntos aleatorios: chunk
38, 97, 236, 351, 409 en distintas corridas) -- no algo nuevo introducido
por el padding. En esa sesión, `--fragment-size 0` (una sola escritura BLE
por frame en vez de fragmentar en 20 bytes) combinado con `--chunk-timeout
15` fue lo que dio más estabilidad.

**Repetir con:**
```
--pad-to-screen-bytes --fragment-size 0 --chunk-timeout 15
```
para intentar llegar de nuevo al `finish` con el payload rellenado y así sí
poder leer la hipótesis.


## 2026-08-31 (misma sesión) — Segundo intento de padding: llegó a 80%, se cortó en otro punto al azar

Con `--fragment-size 0 --chunk-timeout 15` llegó mucho más lejos: chunk
462/576 (80.2%) antes de agotar los 3 reintentos por chunk (15s cada uno,
45s total) y abortar. Otro punto de corte distinto a los anteriores (2,
38, 97, 236, 351, 409, y ahora 462) -- sigue leyendo como inestabilidad de
BLE al azar, no como algo relacionado al contenido/padding en sí.

Sigue sin llegar al `finish`, así que la hipótesis del conteo fijo de bytes
sigue sin poder leerse. El día anterior (2026-08-30), `--inter-chunk-delay-ms
30` fue lo único que había logrado completar el 100% de los 576 chunks una
vez.

**Repetir con:** `--pad-to-screen-bytes --fragment-size 0 --chunk-timeout 15
--retries 8 --inter-chunk-delay-ms 30` -- más reintentos por chunk (barato,
no reinicia toda la subida) y el retraso entre chunks que antes ayudó, para
maximizar la chance de llegar de una vez a los 576/576 y de ahí al `finish`.


## 2026-08-31 (misma sesión) — Hipótesis del conteo fijo de bytes: DESCARTADA

Con `--retries 8 --inter-chunk-delay-ms 30` los 576/576 chunks (115200
bytes totales: 29904 de RU50 real + 85296 de relleno en cero) se
confirmaron sin problema. Resultado en el `finish`: **otra vez silencio
total**, mismo patrón exacto que con el blob de 29904 bytes sin rellenar
(un status=1 que es en realidad el eco del último chunk, heartbeat de
siempre, ningún `status=2` ni rechazo explícito).

Esto **descarta la hipótesis anterior**: si el problema fuera solo que el
firmware espera un conteo fijo de `width*height*2` bytes, rellenar hasta
esa cifra exacta debería haber producido el mismo rechazo explícito y
rápido que vimos con RGB565 puro. No pasó -- el silencio persiste incluso
con el largo "correcto". Conclusión: el comportamiento no depende del largo
total, depende del *contenido*.

**Hipótesis revisada:** el header RU50 (con la marca mágica `RU50` al
principio) probablemente hace que el firmware cambie a un modo de
parseo/decodificación específico para ese formato -- distinto del camino
simple de "sumar bytes y comparar" que usa con datos crudos tipo RGB565.
Si algún campo de ese header (varios son constantes "misteriosas" copiadas
tal cual de una extracción de binario, sin significado confirmado -- ver
`ru50.py`) no calza exactamente con lo que esa rutina de parseo interno
espera, un decodificador embebido con poco manejo de errores podría
quedarse esperando/parseando indefinidamente en vez de fallar rápido --
coincide con que sea precisamente el header lo que dispara ese camino,
mientras que RGB565 (sin marca ni estructura reconocible) sigue el camino
simple y explícito.

**Dos caminos a partir de acá:**
1. Seguir probando variantes del header a ciegas (por ejemplo, poner en
   cero los campos "misteriosos" en vez de los valores extraídos, por si
   son específicos de la versión de herramienta del fabricante y no
   constantes universales) -- barato de probar, pero es adivinar sin más
   evidencia real.
2. Conseguir una captura real de tráfico Bluetooth (HCI) mientras la app
   SuperBand hace una subida de foto real desde el iPhone del usuario --
   terminaría de raíz con la adivinanza, mostrando exactamente qué bytes
   manda la app real. Requiere más esfuerzo de configuración (perfil de
   diagnóstico de Bluetooth de Apple, y idealmente una Mac para leer el
   archivo `.pklg` resultante, aunque también se puede intentar parsear
   directamente).


## 2026-08-31 (misma sesión) — Implementado: probar el header sin los campos misteriosos

Agregado `--ru50-zero-unknown-fields` a `upload-dial`: pone en cero los 6
campos del header RU50 cuyo significado no está confirmado (copiados tal
cual de una extracción puntual del binario del fabricante), dejando intactos
magic/ancho/alto/largo/flags/CRC16. Prueba concreta para el siguiente
intento:

```
--format ru50 --ru50-zero-unknown-fields --fragment-size 0 --chunk-timeout 15 --retries 8 --inter-chunk-delay-ms 30
```

(sin `--pad-to-screen-bytes` esta vez -- ya se descartó esa parte de la
hipótesis, así que probamos solo con los 29904 bytes reales del blob RU50.)

## 2026-08-31 (misma sesión) — Investigación online: ¿qué tan sólido es el formato RU50 que estamos implementando?

Mientras corría la prueba de `--ru50-zero-unknown-fields`, se investigó a fondo
el origen real del formato RU50 en el repo `DynamicDevices/lcd-badge-ble`
(historia completa de git, no solo el estado actual) y dos repos externos
más. Hallazgo importante que cambia la confianza que deberíamos tener en
seguir adivinando el header a ciegas.

**Arqueología de `ru50_convert.py` (el script que portamos a `ru50.py`):**

- El *issue #2* de ese repo (30-abr-2026) trata RU50 como una incógnita
  total: literalmente pregunta si es "¿una marca de tipo de archivo, un ID
  de contenedor/compresión, o una bandera de modo APP→dispositivo?" -- sin
  respuesta de nadie.
- Tres días después (3-may-2026), el mismo autor agrega `ru50_convert.py`
  ya con magic, offsets de header y una tabla CRC16 de 512 bytes
  "extraídos" de una supuesta librería nativa `libjl_bmp_convert.so`
  ("BmpConvert 1.6.0 x86_64, `.rodata` @ 0x9460") -- co-autoría marcada
  como `Cursor <cursoragent@cursor.com>` (un agente de IA). **Esa librería
  .so nunca fue subida al repo**, y el archivo que el propio docstring cita
  como referencia (`../decompile/ENCODER_SPEC.md`) **tampoco existe en
  ningún commit de la historia completa** -- no hay forma de verificar esos
  detalles de forma independiente desde el repo.
- Peor: revisando las 4 versiones históricas de `build_ru50_blob`
  (`c3d7eca` → `e67e473`), **el bug de orden que nosotros encontramos y
  arreglamos (escribir los campos del header y recién después poner en
  cero la región "reservada", pisando esos mismos campos) está presente
  sin cambios en las 4 versiones**, desde el primer commit hasta la
  versión que portamos. Si alguien alguna vez hubiera generado un `.bin`
  real con ese script y mirado el resultado en hexdump, un header
  completamente en cero después del byte 20 (salvo el magic) habría sido
  obvio de inmediato. Esto sugiere fuertemente que **nadie -- ni siquiera
  su propio autor -- llegó a ejecutar este script y revisar su salida**,
  mucho menos a probarla contra el badge real.
- Por separado, el commit que sí tiene respaldo de hardware real
  (`096490d`, 2-may-2026, autor distinto -- `jackghx`, probado en un DG01
  modelo **LJ733 firmware V32399** con una captura real de sysdiagnose de
  iOS de la app SuperBand subiendo una imagen) dice explícitamente: la
  subida llega al 99% (todo el transporte/framing confirmado por captura
  real), pero **"Remaining blocker is RU50 proprietary image format"** --
  es decir, ni siquiera con la captura real en mano ese autor logró
  decodificar el cuerpo de la imagen RU50. No hay ninguna evidencia en el
  repo de que el `ru50_convert.py` posterior se haya comparado alguna vez
  byte a byte contra los bytes reales de esa captura.

**Conclusión:** lo que veníamos probando (magic `RU50`, header de 1104
bytes, los 6 campos "misteriosos", incluso los offsets exactos 0x3C/0x44/
0x4C) es, en el mejor de los casos, una reconstrucción por IA a partir de
decompilar una herramienta relacionada pero distinta (`BmpConvert`), nunca
confirmada por nadie contra hardware real ni siquiera revisada por su
propio autor. Es decir: los "campos desconocidos" podrían no ser campos
reales del formato en absoluto, sino relleno inventado por el proceso de
reconstrucción. Esto baja bastante la probabilidad de que sigamos
adivinando variantes de esos 6 campos y demos con la correcta -- el
problema no es necesariamente "qué valor va en el campo X", sino que la
estructura completa que asumimos podría estar equivocada de raíz.

**Otras dos fuentes revisadas, sin resultado:**
- `kagaimiq/jielie` (wiki independiente de ingeniería inversa de chips
  JieLi): tiene documentación detallada de formatos de filesystem
  (JLFS/SDFILE) y firmware (BR17/newfw) para chips BR/DV/CD/BC, pero cero
  menciones a RU50, ETC2, LJ733, V32399/V35509 o "badge". Son chips de la
  línea de audio, no necesariamente la misma familia del SoC de pantalla
  del badge.
- `Jieli-Tech/fw-AC63_BT_SDK` (SDK oficial de JieLi en GitHub): tampoco
  tiene ninguna mención a RU50, ETC2, ni estructuras de recursos de
  imagen/UI. Es el SDK genérico de la serie AC63 (auriculares/audio BT),
  no parece cubrir el mismo firmware que trae el badge.

**Impacto práctico:** esto no invalida el transporte (cmd 31, start de 17
bytes, finish de 4 bytes, etc. -- eso sí viene de una captura real y
confirmada en hardware idéntico). Lo que pierde piso es específicamente la
receta interna del contenedor RU50. Sigue siendo razonable terminar de
probar `--ru50-zero-unknown-fields` (barato, ya en curso), pero si eso
tampoco da señales, la opción de conseguir una captura BLE real de la app
SuperBand subiendo una foto (opción "b" que se había dejado de lado) pasa
a ser bastante más valiosa que seguir adivinando variantes del header --
ya no es "afinar un formato confirmado", es "reconstruir un formato que
nadie ha confirmado nunca".

## 2026-08-31 (misma sesión) — Resultado de `--ru50-zero-unknown-fields`: falsifica también esta hipótesis

Corrida real:

```
upload-dial BB:50:43:DE:85:96 imagen\reunion.jpg --ru50-zero-unknown-fields --fragment-size 0 --chunk-timeout 15 --retries 8 --inter-chunk-delay-ms 30
```

Transporte: 150/150 chunks confirmados sin un solo reintento (link estable
esta vez). En el `finish`:

- Un `status=1` que, decodificado (`cd0009200101000400000001` = cmd**32**/
  sub**1**), es otra vez el eco tardío del ack del último chunk -- no una
  respuesta real al finish. Mismo patrón ya visto y documentado.
- Después: silencio total salvo el heartbeat/banner ya conocido y sin
  relación (`cd001b15010c0016...`, cmd**21**/sub**12**, payload de puros
  ceros -- el mismo frame que ya se había visto y documentado en la entrada
  "Segunda corrida limpia" de más arriba, no es nada nuevo).
- Igual que siempre: nunca llega `status=2`, nunca hay un rechazo explícito.
  Se agotan los 2 intentos de 45s y falla.

**Resultado: exactamente el mismo comportamiento que con los 6 campos
"misteriosos" en sus valores originales (sin poner en cero).** Esto
**falsifica** la hipótesis específica de la entrada anterior ("Hipótesis
revisada") de que alguno de esos 6 campos concretos era la causa directa
del cuelgue -- si lo fuera, ponerlos en cero (un valor claramente distinto
al original) debería haber cambiado algo: un rechazo distinto, un
`status=2`, o al menos un timing diferente. No cambió nada en absoluto.

**Evaluación combinada con la investigación de hoy (ver entrada anterior
"Investigación online"):** ya van dos hipótesis concretas probadas y
descartadas sobre el contenido de la subida --

1. "El firmware espera un conteo fijo de bytes" -- descartada (rellenar a
   115200 bytes no cambió el resultado).
2. "Alguno de los 6 campos desconocidos del header es la causa" --
   descartada recién ahora (ponerlos en cero no cambió el resultado).

-- sumado a que la arqueología de git mostró que el formato RU50 que
implementamos nunca fue confirmado por nadie contra hardware real, ni
siquiera revisado por su propio autor (ver entrada anterior). La lectura
más honesta en este punto: no es que nos falte encontrar el valor correcto
de un par de campos -- es que **no tenemos ninguna confirmación de que la
estructura completa que asumimos para RU50 sea la correcta**, y seguir
variando constantes al azar dentro de esa estructura asumida tiene cada
vez menos probabilidad de tener éxito, porque el espacio de búsqueda real
podría no ser "estos 6 campos" sino "toda la forma del header".

**Recomendación:** agotar variantes ciegas del header ya no es el camino
más eficiente. El siguiente paso de mayor valor real es conseguir una
captura BLE (HCI) de la app SuperBand real subiendo una foto -- aunque sea
parcial o de menor calidad que un sysdiagnose completo de iOS (por ejemplo,
un teléfono Android con "habilitar registro HCI" en las opciones de
desarrollador, que genera un `btsnoop_hcp.log` legible sin depender de una
Mac) -- para comparar byte a byte contra lo que arma `ru50.build_ru50_blob`
en vez de seguir adivinando a ciegas.

## 2026-08-31 (misma sesión) — Decisión: preguntar en el issue de GitHub en vez de seguir adivinando

Ante la evidencia acumulada hoy (dos hipótesis de header descartadas +
hallazgo de que RU50 nunca fue validado por nadie contra hardware real, ni
siquiera por el autor de `ru50_convert.py`), se decidió NO seguir probando
variantes del header a ciegas por ahora. En su lugar: publicar un
comentario en el issue #2 de `DynamicDevices/lcd-badge-ble`, dirigido en
particular a `jackghx` (quien sí tiene una captura real de sysdiagnose de
iOS en hardware LJ733/V32399 y llegó al 99% de la subida), pidiendo si
puede compartir los bytes reales del payload RU50 de esa captura para
comparar byte a byte contra `ru50.build_ru50_blob`.

Esta sesión no tiene credenciales de GitHub para publicar en nombre del
usuario, así que el comentario quedó redactado en
`docs/github_issue2_comment_draft.md` (en inglés, para la audiencia del
repo) listo para que el usuario lo pegue directamente en
https://github.com/DynamicDevices/lcd-badge-ble/issues/2 .

**Pendiente:** publicar ese comentario y esperar respuesta. Mientras tanto,
como alternativa que no depende de que alguien responda, sigue en pie la
opción de conseguir una captura HCI propia (por ejemplo con un Android que
tenga "habilitar registro HCI" en opciones de desarrollador) usando la app
SuperBand real para subir una foto.

## 2026-08-31 (misma sesión) — El fix real: header RU50 desensamblado desde el binario oficial de JieLi

Se recibió (vía el issue de GitHub / un análisis de terceros dirigido a
"Jack") un reporte de análisis estático serio sobre `libjl_bmp_convert.so`
-- con `readelf`/`objdump`/`strings` reales, no una reconstrucción por IA
sin verificar como la anterior. Antes de confiar en el contenido, se
verificó todo de forma independiente:

- Se clonó `Jieli-Tech/Android-JL_Bluetooth` (repo oficial de JieLi) y se
  confirmó que `libs/BmpConvert_V1.6.0_10605-release.aar` existe
  exactamente como se describía -- es un zip real, se pudo extraer.
- Dentro del AAR: `jni/x86_64/libjl_bmp_convert.so` (ELF real, 448 KB).
  `readelf -sW` confirma exactamente los símbolos descritos:
  `br35_bmp_to_res`, `br35_bmp_to_res_path`, `br28_*`, `br23_*`,
  `bmpConvert` (el dispatcher), y todos los símbolos ETC2/THUMB58H/
  THUMB59T de `compressBlock*`/`decompressBlock*`.
- Se desensambló `br35_bmp_to_res` (offset 0x12280, 1606 bytes) a mano con
  `objdump -d -Mintel` y se leyó instrucción por instrucción la
  construcción completa del header RU50.

**Dos errores concretos encontrados en `ebadge/ru50.py` (ninguno tenía que
ver con los 6 campos "misteriosos" que se venían sospechando):**

1. `HDR_QW_04` estaba mal: el valor real (confirmado por
   `movabs rax, 0x0000000100010500` en la instrucción real) tiene las dos
   mitades de 16 bits del medio invertidas respecto a lo que veníamos
   usando (`0x0000000100050100`).

2. **El orden de los campos desde el offset 0x3C estaba mal -- este es
   probablemente el motivo real del silencio en `finish` que se viene
   arrastrando desde el primer intento con RU50.** El desensamblado
   muestra la escritura real, en este orden exacto:
   - offset 0x3C (u16): `crc_header`
   - offset 0x3E (u16): `crc_payload`
   - offset 0x40 (u32): flags (`0x00920001`, calculado como `A+B+1` según
     una rama del código; el valor que veníamos usando parece ser el
     correcto para el camino "normal", pero queda documentada la
     alternativa `0x00228001` si hiciera falta)
   - offset 0x44 (u16): ancho
   - offset 0x46 (u16): alto
   - offset 0x48 (u32): **el largo real del payload ETC2**
   - offset 0x4C (u32): **una constante fija = 0x450** (el tamaño del
     propio header) -- **no el largo del payload otra vez**, como
     asumíamos.

   Nuestra versión anterior ponía flags en 0x3C, un word combinado
   `(crc_header<<16)|crc_payload` en 0x40, y el largo del payload en
   0x4C en vez de la constante 0x450. Si el parser del firmware espera
   leer el largo real del payload en 0x48 y en cambio ahí había flags (o
   viceversa, el firmware interpreta lo que hay en 0x4C como el largo del
   payload y ahí encuentra 0x450 en vez del largo real), tiene sentido
   que se quede esperando indefinidamente una cantidad de datos que nunca
   llega -- coincide exactamente con el síntoma observado (chunks OK,
   silencio total e indefinido en `finish`, nunca un rechazo explícito).

**También corregido:**
- La zona "reservada" es `[0x50, 0x450)`, no `[0x14, 0x414)`. El
  desensamblado muestra un `memset` de un buffer de 0x400 bytes en cero,
  copiado con `memcpy` a `output+0x50` -- justo donde termina el último
  campo del header (0x4C+4=0x50), sin ningún solapamiento. La versión
  anterior "arreglaba" un solapamiento que en el código real nunca existió
  (era un error de traducción del script que portamos, no del firmware).
- Los 18 bytes que alimentan el segundo CRC16 (`crc_header`) llevan datos
  reales en este orden: `crc_payload` (u16), flags (u32), ancho (u16),
  alto (u16), largo del payload (u32), la constante 0x450 (u32) -- no
  empiezan con flags ni terminan en un relleno en cero como asumíamos.

**Verificado independientemente (no solo se copió el análisis recibido):**
- La tabla CRC16 de 512 bytes en `.rodata` @ offset 0x9460 del `.so` se
  volcó directo con `objdump -s` y es byte a byte idéntica a la que ya
  teníamos en `ebadge/ru50.py` -- esa parte SÍ estaba bien desde el
  principio.
- Se desensambló también `Crc16` (offset 0x14000) y se confirmó que el
  algoritmo (nibble alto primero, `crc=((crc<<4)&0xFFFF)^tabla[((crc>>12)^nibble)&0xF]`)
  coincide exactamente con lo ya implementado.
- Las 6 constantes "de significado desconocido" (`HDR_QW_18/20/28/30`,
  `HDR_DW_38`) resultaron ser byte a byte idénticas a lo que ya
  teníamos -- ese no era el problema, tal como ya habían mostrado las
  pruebas de `--ru50-zero-unknown-fields`.

**Estado:** `ebadge/ru50.py` fue reescrito con el layout corregido,
`tests/test_ru50.py` actualizado (20/20 tests pasan). Esta es la primera
vez que el header RU50 se arma a partir de desensamblado directo y
verificado del binario real del fabricante, no de una reconstrucción de
terceros sin verificar. Pendiente: **probar contra el badge real** -- todo
lo anterior está confirmado a nivel de bytes/lógica, no de hardware.

**Próximo paso:** repetir la subida real con el `ru50.py` corregido:

```
uv run python main.py --debug upload-dial BB:50:43:DE:85:96 imagen\reunion.jpg
```

(sin `--ru50-zero-unknown-fields` esta vez -- ya se descartó esa
hipótesis, y ahora las 6 constantes están en sus valores reales
confirmados por desensamblado, no por una extracción de terceros sin
verificar).

## 2026-08-31 (misma sesión) — El fix por desensamblado tampoco cambió nada: mismo silencio exacto

Corrida real con el `ru50.py` corregido (offsets desde desensamblado real,
sin `--ru50-zero-unknown-fields`):

```
uv run python main.py --debug upload-dial BB:50:43:DE:85:96 imagen\reunion.jpg
```

Resultado: **byte por byte el mismo patrón que con todas las versiones
anteriores.** 150/150 chunks confirmados, el mismo `status=1` que es el eco
del último chunk, el mismo heartbeat `cmd21/sub12` sin relación, dos
intentos de 45s, mismo `gave up`. Ningún cambio observable pese a que esta
vez el header corregido está verificado contra el binario real del
fabricante (desensamblado, no una reconstrucción de terceros).

**Esto es información valiosa, aunque negativa.** Ya van CUATRO variantes
de contenido probadas, todas con el resultado idéntico:
1. RGB565 crudo -- rechazo instantáneo y explícito (`status=1`, distinto a
   todo lo demás).
2. RU50 con el header original (offsets del script de terceros) -- silencio.
3. RU50 con relleno a tamaño fijo -- silencio.
4. RU50 con los 6 campos "desconocidos" en cero -- silencio.
5. RU50 con el header corregido por desensamblado real -- **silencio,
   idéntico**.

Que la corrección (2)→(5), verificada contra el binario real y con motivo
concreto (el orden de campos estaba mal), no haya cambiado NADA es un dato
importante en sí mismo: sugiere que el parser de la firmware del badge no
está siquiera llegando al punto de leer esos campos con matices distintos
entre header viejo/nuevo, o que el bloqueo real está en otro lugar
completamente distinto al contenido del archivo.

**Nueva hipótesis, también salida del mismo desensamblado de hoy:** la
función real `br35_bmp_to_res` tiene una rama "sin empaquetar" (el mismo
desensamblado muestra `test BYTE PTR [rbp-0x450],0x0` -- un flag de
"pack" que, si es falso, salta a un camino mucho más simple: un `memcpy`
directo de la textura ETC2 comprimida al buffer de salida, **sin magic,
sin header, sin CRC alguno** -- ver `br35_bmp_to_res_path_nopack` /
`br28_bmp_to_res_path_nopack` en los símbolos del `.so`). Nunca se probó
mandar la textura ETC2 pelada, sin el contenedor RU50 completo. Se agregó
`--format etc2-nopack` a `upload-dial` para probar exactamente esto:

```
uv run python main.py --debug upload-dial BB:50:43:DE:85:96 imagen\reunion.jpg --format etc2-nopack
```

Para 240x240 esto manda 28800 bytes (144 chunks) en vez de 29904 (150
chunks) -- los mismos bytes de textura ETC2, solo que sin los 1104 bytes
de header/CRC alrededor.

**Sigue en pie, y ahora con más peso, conseguir una captura real** de la
app SuperBand subiendo una foto de verdad -- cuatro hipótesis de contenido
descartadas con el mismo síntoma exacto es una señal fuerte de que seguir
adivinando el contenido del archivo, aunque sea con buena base técnica,
tiene rendimientos decrecientes.

## 2026-08-31 (misma sesión) — Re-auditoría de la documentación: encontrado un formato de frame completo que nunca implementamos

A pedido explícito de volver a revisar la documentación de los repos ya
clonados (no solo el código nuevo), se releyó con más cuidado
`dg01-ble/src/dial_upload.rs` y `main.rs` completos, no solo los fragmentos
citados hasta ahora. Apareció algo que cambia el diagnóstico de raíz.

**Hallazgo 1 (menor, de contexto):** el valor por defecto de `mid4`
(`15a20008`) en `dial_start_extended` fue introducido citando
`logs/upload-2.log.pcapng` como "captura exitosa de la app real" -- pero
un commit del MISMO día (`a2efc96`, "docs: PCAP analysis") documenta que
ese mismo archivo **no contiene ningún tráfico `cmd 31`** (ni start, ni
chunks, ni finish -- solo preflight y clima). Es decir, el propio proyecto
se contradice sobre el origen de ese valor, y nadie lo corrigió después
(el commit de mayo con la captura real de iOS -- `096490d` -- tocó el
framing del finish y el largo del start, pero no volvió a tocar `mid4`).
Sigue siendo un valor plausible (viene de *algún* lado), pero su
justificación documentada no se sostiene. Hay una función alternativa ya
lista, `dial_start_mid4_dims_be(width, height)`, que deriva `mid4` de las
dimensiones reales en vez de usar el valor fijo -- útil si el problema
resultara estar ahí, aunque el hallazgo 2 de abajo es mucho más prometedor.

**Hallazgo 2 (el importante): un formato de notificación completo que
nunca portamos.** `dial_upload.rs` documenta que el firmware puede
contestar con frames cortos de 8 bytes que empiezan con **`0xDC`** (no
`0xCD`) -- un camino de notificación totalmente separado
(`BaseReceiveData`'s "other branch" en la app, nunca mezclado con el
protocolo `0xCD`). Ejemplos reales capturados:

- `dc 00 05 15 0c 00 1e 01` -- cmd=0x15, sub=0x0c. **Es exactamente el
  mismo cmd/sub del frame que venimos descartando como "heartbeat/banner
  sin relación" en cada corrida** (nuestra versión llega envuelta en un
  frame `0xCD` de 22 bytes de payload en cero, pero el cmd/sub es
  idéntico).
- `dc 00 05 1f 02 00 19 01` -- cmd=31 (`CMD_DIAL_TRANSFER`), sub=2
  (`SUB_DIAL_START`). El commit `096490d` (la captura real de sysdiagnose
  de iOS en hardware LJ733) dice textual: **"DC frame ACK: accept dc 00 05
  1f 02 00 19 01 as valid start ACK"**.

Y en `main.rs`, la función que espera acks (`await_dial_ack` o equivalente)
tiene esta regla explícita: un frame `0xDC` con **cmd=31/sub=3 cuenta como
ACK válido de `finish`** (`want==2`), totalmente aparte de cualquier
paquete `0xCD` con un entero de status. También cmd=0 con sub igual al
número de chunk cuenta como ack de ESE chunk.

**Por qué esto es gigante:** nuestro `CdNotifyAssembler` (`ebadge/protocol.py`)
sólo reconoce frames que empiezan con `0xCD`. Cualquier notificación que
empezara con `0xDC` era **descartada en silencio, sin ningún rastro en el
log** (`push()`: si el primer byte no es el marcador esperado, busca el
siguiente `0xCD` y si no lo encuentra, vacía el buffer completo). Nuestro
`_on_notify` sólo logueaba bytes que ya habían sido reconocidos como frame
completo -- nunca los bytes crudos. Es decir: **si el ack real de `finish`
para este firmware llega como un frame `0xDC` corto en vez de un `status=2`
en formato `0xCD`, nuestro programa nunca lo habría visto, nunca lo habría
logueado, y se habría quedado esperando indefinidamente algo que en
realidad sí llegó** -- exactamente el síntoma que venimos observando
(silencio total, nunca un rechazo explícito) en las CUATRO variantes de
contenido RU50 probadas hasta ahora, todas con el resultado idéntico. Que
el contenido del archivo no haya importado nunca tiene mucho más sentido
si el problema no era el contenido, sino que estábamos ciegos a la
confirmación de éxito.

**Corregido:**
- `ebadge/protocol.py`: agregado `FRAME_MARKER_DC = 0xDC`,
  `parse_dc_short()` (puerto directo de la función Rust homónima) y
  `is_dc_ack_for(cmd, sub, want_status)` (puerto de la regla de aceptación
  que usa `dg01-ble` para start/finish/chunk).
- `ebadge/ble.py`: `_on_notify` ahora loguea **todo** byte crudo recibido
  (`<< raw ...`) antes de intentar interpretarlo -- para no volver a
  quedar ciegos a un formato que no reconocemos -- y detecta frames `0xDC`
  cortos, encolándolos para que `_await_ack` los vea. `_await_ack` ahora
  chequea primero si el frame es un `0xDC` corto y, si su cmd/sub
  corresponde al `want_status` que se está esperando (start/finish/chunk),
  lo acepta como ack válido inmediatamente.
- `tests/test_protocol.py`: 3 tests nuevos para `parse_dc_short` /
  `is_dc_ack_for` (23/23 tests pasan en total, verificado en un venv
  aislado fuera de la carpeta del proyecto).

**Próximo paso:** repetir la subida real (sin cambiar nada del formato RU50
-- ya está en su versión corregida por desensamblado del turno anterior):

```
uv run python main.py --debug upload-dial BB:50:43:DE:85:96 imagen\reunion.jpg
```

Con el nuevo logueo de `<< raw ...`, esta corrida va a mostrar por primera
vez si el badge efectivamente manda algún frame `0xDC` durante el `finish`
que antes pasaba completamente desapercibido. Si aparece uno con cmd=31/
sub=3, `_await_ack` ahora lo va a aceptar automáticamente como éxito. Si no
aparece ninguno, al menos vamos a poder confirmarlo con certeza en vez de
seguir asumiendo silencio total.

## 2026-08-31 (misma sesión) — Releída la letra chica del issue #2: nadie lo comentó todavía, y pide cosas que nunca probamos

A pedido explícito de "revisar con mayor detalle" el issue
https://github.com/DynamicDevices/lcd-badge-ble/issues/2, se volvió a
traer el cuerpo COMPLETO y verbatim (no un resumen) y se confirmó el
estado real de los comentarios.

**Dato importante: el issue sigue sin ningún comentario.** Se verificó
dos veces -- la página no muestra ningún comentario de nadie, incluido
`jackghx`. Esto significa que el análisis estático de
`libjl_bmp_convert.so` que compartiste ("for Jack") **no vino de este
issue** -- debe haber llegado por otro canal (¿le escribiste directo a
Jack, o es de otra fuente?). Vale la pena confirmar esto porque cambia
cómo seguimos: si tenés forma de contactar a Jack directamente, la
pregunta 5 del issue original (ver abajo) sería el pedido más valioso que
se le puede hacer.

**El cuerpo original del issue (de `ajlennon`, reportando lo que
`jackghx` encontró) dice algo que no habíamos registrado con el peso que
merece:** *"El stock app puede llevar una subida a ~100% de la UI y ahí
se cuelga"* -- es decir, **la app oficial real, con el firmware/encoder
real, exhibe el mismo síntoma de congelamiento que nosotros**. Esto no
prueba que nuestro formato esté bien, pero sí abre la posibilidad real de
que el "silencio al final" no sea (solo) un bug de nuestra
reimplementación -- podría ser un comportamiento genuino de esta
variante de firmware/hardware que ni siquiera el vendor resolvió del
todo, al menos en el equipo de prueba de Jack.

**El issue pide explícitamente 5 cosas para decidir el siguiente paso.
Repasando cuáles ya tenemos y cuáles no:**

1. *Definición de RU50 con un hex de ejemplo* -- ya lo resolvimos nosotros
   solos, con más detalle del que el propio issue tenía (desensamblado
   real de `br35_bmp_to_res`, ver entradas de más arriba).
2. *Caracterización de la pérdida de paquetes en las capturas* -- no
   aplica a nuestro caso: nosotros somos la app, tenemos visibilidad
   completa de cada byte que mandamos y recibimos, no dependemos de un
   sniffer externo con huecos.
3. **"¿Al llegar a 100%, el dispositivo sigue notificando algo (status/
   error) o el tráfico se corta por completo?"** -- **esto ya lo podemos
   contestar con nuestros propios datos**, y es una respuesta útil para
   aportar de vuelta al issue: el tráfico NO se corta del todo -- sigue
   llegando un frame periódico (`cmd 0x15/sub 0x0c`, "banner", cada ~30s)
   y una vez un eco tardío del último chunk -- pero nunca un cambio de
   estado real ni un rechazo explícito.
4. **"¿Una carga sintética mínima (1x1 o las dimensiones de dial más
   chicas permitidas), tageada/formateada como la app, se completa? ¿O
   la falla solo pasa con transferencias a resolución completa?"** --
   **nunca probamos esto.** Es barato de probar con lo que ya tenemos
   (`--width`/`--height` ya se pueden forzar de forma independiente del
   tamaño real de pantalla del badge) y responde algo importante: si un
   RU50 chico se completa bien, el problema es de tamaño/tiempo, no de
   formato -- si también cuelga igual, refuerza que el problema es de
   protocolo/formato y no de tamaño.
5. *Un asset "de verdad" (exportado por el fabricante o de la propia
   APK) para comparar byte a byte el primer chunk* -- no lo tenemos. Si
   tenés forma de pedírselo a Jack directamente (ya que él llegó al 99%
   con captura real), sería el dato más valioso posible en este momento.

**Próximo test sugerido (pregunta 4 del issue, nunca probada):**

```
uv run python main.py --debug upload-dial BB:50:43:DE:85:96 --solid 255,0,0 --width 8 --height 8
```

(un cuadrado sólido de 8x8, mucho más chico que la pantalla real de
240x240 -- el firmware podría rechazarlo por tamaño, lo cual también
sería información útil y explícita, a diferencia del silencio actual.)
