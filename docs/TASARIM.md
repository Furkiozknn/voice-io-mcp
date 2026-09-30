# Tasarım: voice-io-mcp ilk kullanım ve README yenilemesi (30 Eylül 2026)

## Hedef

Videodan ya da profilden gelen biri ilk dakikada şunu yapabilmeli: aracın ne yaptığını tek cümlede anlamak, tek komutla kurmak, anahtarı ya da yerel modeli gerektiğini baştan bilmek; bağlanan sunucunun gerçekten cevap verdiğini görmek. Çekirdek davranış (fallback zinciri, yol güvenliği, hata sözleşmesi, `isError`) değişmedi; sürüm numarası artmadı (0.1.0).

## Önce / sonra

| Konu | Önce | Sonra |
|---|---|---|
| README ilk ekranı | banner, üreticisiz 15 sn'lik reel, dört rozet, iki paragraf, sonra kurulum | banner, tek cümlelik tanım, tek satır kurulum, anahtar/yerel model gereği dürüstçe, 16 sn gerçek çıktılı terminal demosu, ölçülmüş süre, "ne zaman kullanılır / kullanılmaz" tablosu, sonra rozetler ve eski gövde |
| Demo | kaynağı depoda olmayan reel | `scripts/demo-uret.py`: 4 komut gerçekten koşulur, `docs/demo/komutlar.txt` kayıttır, sayfa o kaydı yazma animasyonuyla oynatır; `demo-kayit.js` mp4/gif alır |
| Sunucunun ilk yanıtı | 15-17 s (litellm içe aktarması) | 4-4,4 s; litellm ilk kullanımda yüklenir |
| Komut satırı | argümanlar yok sayılır, sunucu başlar | `--help`, `--version`, `--check` (çıkış 0/1), bilinmeyen bayrak çıkış 2 |
| Sağlık kontrolü | `uvx ... python -c "import asyncio, voice_io; ..."` | `voice-io-mcp --check` |
| Dizin yolu (Windows) | "Permission denied" | "is not a regular file" (Linux ile aynı) |
| Uzantısız yol | `has (no extension), which is not ...` | `has no extension: not a recognized audio format` |
| MCP oturumunu görmek | testler dışında yol yoktu | `scripts/probe.py`: `initialize`, `tools/list`, `--call` |
| Test | Linux CI 60/3 atlama; Windows 56 geçti, 1 kaldı | Linux CI 69/3; Windows 66/6 atlama (+9 test) |

## CLI akışı

```
kur          claude mcp add voice-io [-e GROQ_API_KEY=...] -- uvx --python 3.11 --from git+... voice-io-mcp
             (ya da kaynaktan: git clone; uv sync; claude mcp add ... uv run --project ... voice-io-mcp)
bak          voice-io-mcp --check          her araç için en az bir OK: çıkış 0; yoksa 1
             python scripts/probe.py       sunucuyu stdio'da başlatır, initialize + tools/list gösterir
kullan       Claude Code: "Transcribe ~/Desktop/note.wav", "Read this paragraph out loud"
yanlış       voice-io-mcp --bogus          unknown argument + --help'e yönlendirme, çıkış 2
             araç hatası                   isError: true + iki nedeni de sayan mesaj
```

Sunucu bayraksız çağrıldığında (istemcilerin yaptığı gibi) eskisi gibi stdio'da çalışır; bayraklar yalnızca insanlar için.

## Görsel dil (video sisteminden alınanlar)

Terminal demosu FRK-OS kimliğinde; `mcp-vet` yenilemesinin (`D:\Claude Projeleri\proje-yenileme\mcp-vet\docs\TASARIM.md`) sahnesi ve yazı tipleri aynen alındı, tek fark renklendirme kuralları (`FAIL` mercan, `OK` camgöbeği, `isError=true` turuncu, `voice-io-mcp:` hata öneki mercan).

| Ne | Nereden | Nerede |
|---|---|---|
| `zemin #0e0d0b`, panel `#14120e`, `yazi #f1ece2`, ilk vurgu `#ffc21a` | `sosyal/uret/tema.mjs` `klasik.akis` | terminal sahnesi zemini, panel, metin, sarı `$` isteği ve sol çizgi |
| `#ff4d6d`, `#ff7a1a`, `#19d3e6` | `tema.mjs` klasik vurgular | yalnız boyama; metin değişmez |
| `doku: "izgara"` | `tema.mjs` | gövdede çok soluk ızgara |
| JetBrains Mono | `tema.mjs` `F.jb` | tüm terminal metni; SIL OFL 1.1, `assets/yazi/` (lisans metniyle) |
| yazma animasyonu (30 ms/harf), satır satır çıktı | `sahne.js` terminal tekniği | `scripts/demo-uret.py` sayfası |

Kontrast (panel `#14120e` üstünde, WCAG göreli parlaklıktan; `mcp-vet` TASARIM.md'deki hesapla aynı renkler): krem 15,9:1, sönük metin 8,5:1, sarı 11,6:1, mercan 5,8:1, turuncu 7,2:1, camgöbeği 10,3:1; hepsi ≥ 4,5:1. `prefers-reduced-motion`'da imleç yanıp sönmesi kapalı.

## Kararlar ve sınırlar

- **Banner değişmedi**: `assets/banner.svg` hesabın banner üreticisinden; elle değiştirmek üreticinin sonraki çıktısında silinir.
- **Reel çıkarıldı** (`DENETIM.md`): üretici yok; git geçmişinde duruyor.
- Demo anahtarsız ve ekstrasız durumu gösterir, çünkü kurulumdan hemen sonraki gerçek durum bu ve bu makinede yalnız o ölçülebildi. Ses üretimi/transkripsiyon çıktısı gösterilmez: Groq anahtarı yok, yerel modeller kurulmadı (`DENETIM.md`); uydurma çıktı konmadı.
- Demo dikey 1080x1920 kaydı (`demo-dikey.mp4`) depoya girmedi; günlük video hattı için `sosyal/medya/projeler/voice-io-mcp/terminal.mp4` altında.
- `voice_io.main()` artık çıkış kodu döndürüyor ve `sys.exit(main())` ile çağrılıyor; `[project.scripts]` girişi (`voice_io:main`) bunu zaten `sys.exit` ile sarar, geriye uyumlu.
- Sürüm, etiket, PyPI, dizin/awesome-list başvurusu, MCP Registry ve Pages **yapılmadı** (onay kapısı). `project-meta.json`: test sayısı 60 → 69 ve medya yolu; başka alan değişmedi.
