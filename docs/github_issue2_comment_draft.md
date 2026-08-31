# Borrador de comentario para DynamicDevices/lcd-badge-ble#2

Redactado 2026-08-31. Pendiente: publicarlo (requiere una cuenta de GitHub;
esta sesión no tiene credenciales para postear en nombre de nadie). Ir a
https://github.com/DynamicDevices/lcd-badge-ble/issues/2 y pegar el texto
de abajo como comentario nuevo.

---

Hi — following up on the RU50 image format question here, since I've hit a wall from a different angle and it might help whoever's still stuck on this.

**Setup:** independent Python/`bleak` port of this project's protocol for Windows (not a fork of `dg01-ble`'s Rust code, but ported line-for-line from it), tested against a badge with board `LJ733_MB_V1.1`, firmware `V35509` (via GATT Device Information). So basically the same hardware family as the commit that got to "99%" (`096490d`, firmware `V32399`), just a slightly newer firmware build.

**What I did with `ru50_convert.py`:** ported `build_ru50_blob` and found (and fixed, in my own port) a bug that's present unchanged across all 4 historical commits of that script: it writes the header fields (magic, width/height, payload length, flags+CRC) and *then* zero-fills the "reserved" region `[0x14, 0x414)` afterward — which overlaps and wipes out several of those fields it just wrote (everything from offset 0x18 onward, including width/height/payload-length/CRC at 0x3C/0x44/0x4C). A single hexdump of that script's own output would have caught this immediately, which makes me think nobody — including the script's own author — ever actually ran it and looked at the result, let alone tested the produced `.bin` against a real badge. I flipped the order (zero first, then write fields) in my port, which at least produces an internally-consistent header, but that's a guess about intent, not a confirmed fix.

**Result on real hardware** (with the reordering fix, ETC2 payload via `etcpak`, correct CRC16 via the embedded vendor nibble table): all chunks upload and ack cleanly (150/150 for a 240x240 image), but the `finish` step (`cmd 31 / sub 3`) gets **total silence** — no `status=2` success, no explicit rejection, nothing but the usual unrelated heartbeat notifications, even after waiting 5 minutes connected. This is a different failure mode than raw RGB565, which got an instant, explicit `status=1` ("check failed") rejection every time. I also tried:
- Padding the ETC2 payload to a fixed `width*height*2` byte count (in case the firmware expects that total regardless of declared length) — no change, same silence.
- Zeroing the 6 "constant, meaning unknown" header fields (`HDR_QW_04/18/20/28/30`, `HDR_DW_38` in the script) instead of using the captured values — no change either, same silence.

Given the header-clobbering bug above, and that I can't find any evidence in the repo's history that `ru50_convert.py`'s output was ever compared byte-for-byte against a real captured RU50 payload (the `libjl_bmp_convert.so` it's supposedly extracted from was never committed, and the `decompile/ENCODER_SPEC.md` its docstring references doesn't exist in any commit either), I think the header layout in that script may not be reliable at all — possibly not just "a couple of unknown fields wrong" but the overall structure being off.

**@jackghx** (or anyone else who's captured a real upload) — since `096490d`'s commit message mentions an iOS sysdiagnose capture that got the transport to 99%: would you be able to share the raw bytes of the RU50 image payload from that capture (or even just the first 0x450 bytes / the header)? That would let us compare byte-for-byte against `build_ru50_blob`'s output instead of continuing to guess blindly. Happy to share my own port/findings (including the full writeup of everything above) if useful — just say the word.
