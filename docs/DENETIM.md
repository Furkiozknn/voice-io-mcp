# Denetim: voice-io-mcp (30 Eylül 2026)

Yenilemeden önce `main` (0.1.0, `9709560`) üzerinde, bu makinede (Windows 11, Python 3.11 venv, uv 0.12.5, Git Bash) ölçüldü. Ölçülmeyen bir şey yazılmadı. Ham çıktılar depo dışında: `kanit/voice-io-mcp/{once,sonra}/` (aynı 6 komut, iki sürüme karşı).

## Ne ölçülemedi

- **Groq'un canlı uçları**: anahtar yok, istenmedi. `check_provider_health` ve `text_to_speech`/`speech_to_text`'in Groq yolu bu makinede çalıştırılmadı. README'nin "model adları canlı doğrulanmadı" cümlesi doğru ve durduğu gibi kaldı.
- **Yerel modeller (Kokoro, faster-whisper)**: bu makinede kurulmadı. Kokoro torch çeker (README: Linux'ta yaklaşık 6 GB), faster-whisper `base` ağırlıkları Hugging Face'ten iner; izin gerektiren büyük indirme sayıldı. Gerçek modelle çalıştığının kanıtı CI'nin `yerel` işi: bu dalın koşusunda `3 passed` (Linux, ekstralar ve espeak-ng kurulu, atlama yok).
- `claude mcp add ...` satırları: kullanıcının Claude yapılandırmasını değiştirdiği için koşulmadı. Aynı sunucu başlatma komutu (`uvx --python 3.11 --from git+... voice-io-mcp`) ve MCP el sıkışması `scripts/probe.py` ile stdio üzerinden gerçekten koşuldu.

## Temiz ortamda kurulum ve ilk sonuç

Her satır boş uv önbelleğiyle, boş klasörde koşuldu (`kanit/.../sonra`, `docs/demo/kurulum.txt`).

| Yol | Süre | Sonuç |
|---|---|---|
| `uvx --python 3.11 --from git+https://github.com/Furkiozknn/voice-io-mcp voice-io-mcp ...` (boş önbellek, `main`) | 45,4 s (ilk koşu); 82 paket, litellm ve openai dahil | kuruldu |
| aynısı `yenileme/arayuz` dalından, boş önbellek | 28,2 s ve 24,0 s | `voice-io-mcp 0.1.0` |
| aynısı, önbellek sıcak | 5,3 s ve 5,8 s | aynı |
| `git clone` + `uv sync --group dev` (from-source yolu) | 29 s | 72 test toplanır |

Kurulum süresi ağa ve önbelleğe çok bağlı (aynı komut için 24 s ile 45 s arası görüldü). "Tek komut, bir dakika": kurulum + ilk sunucu yanıtı ~30-50 s, tutuyor.

## Hata: sunucu ilk yanıtını 15-17 s sonra veriyordu

`voice-io-mcp` bir istemci tarafından başlatıldığında `initialize`'a yanıt vermeden önce `import voice_io` bitiyordu ve bu 15-16 s sürüyordu (`python -X importtime`: `litellm` tek başına 14 s). Claude Code varsayılan olarak bir sunucuya 30 s verir; makine biraz yavaşsa ya da diskte soğuksa sunucu "bağlanamadı" görünür, ve kullanıcıya nedeni söylenmez.

| | Önce | Sonra |
|---|---|---|
| `import voice_io` | 15,6 s | 3,7 s |
| `initialize` yanıtı (`probe.py`, stdio) | 15,8 s / 16,1 s | 4,1 s / 4,4 s |
| `voice-io-mcp --version` | 15,2 s, çıktı yok | 4,3 s, `voice-io-mcp 0.1.0` |

Düzeltme: `litellm` ilk kullanımda yüklenen tembel bir vekil (`_LazyLitellm`). Testlerin `voice_io.litellm.aspeech` yamaları aynen çalışıyor; `suppress_debug_info` yükleme anında ayarlanıyor (stdout temiz kalma testi geçti). Yeni test: `import voice_io` litellm yüklemiyor.

## `--help` / `--version` yoktu

`voice-io-mcp --help`, `--version` ya da `--bogus`: hepsi sessizce sunucuyu başlatıp stdin'de JSON-RPC bekliyordu (elle çalıştırınca terminal 15 s sonra hiçbir şey basmadan dönüyor, çıkış 0; stdin kapalıyken). README'nin sağlık kontrolü de `uvx ... python -c "import asyncio, voice_io; print(asyncio.run(voice_io.check_provider_health()))"` idi.

Şimdi: `--help` (bayrakları ve kaydı anlatır), `--version`, `--check` (sağlık raporu; her iki araçta da en az bir `OK` varsa çıkış 0, yoksa 1), bilinmeyen bayrak çıkış 2 ve `--help`'e yönlendirir. Bayraksız çağrı (istemcinin yaptığı) değişmedi.

## Hata: dizin adı Windows'ta yanlış nedenle reddediliyordu

`speech_to_text` `album.wav` adında bir dizinle çağrılınca Windows `os.open` "Permission denied" veriyordu; mesaj "Cannot open ...: Permission denied" idi. Linux'ta aynı girdi "is not a regular file" (mevcut test `test_rejects_a_directory_with_an_audio_name` bunu bekliyor) ve **Windows'ta bu test kırmızıydı** (56 geçti, 1 kaldı; CI yalnız Linux koştuğu için görünmüyordu). Şimdi iki platformda da "Rejected: ... is not a regular file". Test gevşetilmedi.

## Hata mesajları

| Girdi | Önce | Sonra |
|---|---|---|
| uzantısız yol (`.env`) | `path has (no extension), which is not ...` | `path has no extension: not a recognized audio format (...)` |
| olmayan dosya, boş metin, `GROQ_API_KEY` yok | doğru ve anlaşılır (`File not found`, `text must not be empty`, iki nedeni de sayan mesaj) | değişmedi |
| `--bogus` | sunucu başlar | `unknown argument`, `run voice-io-mcp --help`, çıkış 2 |

Araç hataları `isError: true` ile dönüyor; SDK'nın "Error executing tool ..." öneki mesajın başında kalıyor (SDK davranışı, dokunulmadı).

## MCP sunucusu olarak: initialize, tools/list, araç çağrıları (gerçek stdio)

`python scripts/probe.py --call ...` sunucuyu başlatır, `initialize` + `tools/list` + verilen `tools/call`'ları gönderir. Çıktı `docs/demo/komutlar.txt`'te: 4 araç (`text_to_speech`, `speech_to_text`, `list_voices`, `check_provider_health`), stdout'ta yalnız JSON-RPC (probe satırı JSON değilse düşer). Anahtarsız ve ekstrasız: `.env` yolu yerelde reddedilir, `text_to_speech` iki nedeni sayan hatayla döner. `serverInfo`'da sürüm alanı boş (MCP SDK'nın `MCPServer("voice-io")` çağrısı sürüm vermiyor); dokunulmadı.

## mcp-vet ile denetim

`mcp-vet` (`744baea`, `audit --offline --path`), aynı `main` kaynağına (`kanit/.../once/mcp-vet-audit.txt`): genel risk **MEDIUM**, çıkış 1.

- Araç açıklamalarında ve dizgilerde yönlendirme/enjeksiyon bulgusu yok; ağ hedefleri `api.groq.com` (README'de adı geçiyor).
- `MEDIUM` "local files read near an outbound request" (`voice_io.py:385`, `:415`): beklenen ve README'nin "What this server can actually do" bölümünde anlatılan bulgu. `speech_to_text` adlandırılan bir dosyayı Groq'a yükler; uzantı/symlink/tür/boyut denetimleri bunu sese kısıtlar.
- **Yanlış pozitif**: `voice_io.py:51`'deki yorum ("`pip install`s this server") yüzünden `Installation: HIGH` ve "Installs packages by shelling out at runtime (only in a denylist or a comment)". Yorum yeniden yazıldı; yeni sürümde `voice_io.py` bu bulguyu üretmiyor. Bulgu artık yalnızca `scripts/` (geliştirici betikleri: uvx/playwright/ffmpeg çağıran demo üreticisi) için ve "outside the shipped server" olarak işaretli. Bu bir mcp-vet iyileştirme fikri (yorum içinde geçen komutu HIGH saymasın); bu depoda değil.

## README bulguları

- "15 saniyelik hareketli reel" (`docs/reel/reel.{gif,mp4}`): üreticisi depoda yok, yeniden üretilemez. **README'den ve depodan çıkarıldı** (git geçmişinde `a539cf8`'de duruyor); yerine `scripts/demo-uret.py` ile gerçek çıktıdan üretilen demo geldi.
- İlk ekran: banner, reel, dört rozet, iki paragraf, sonra kurulum; anahtarın gerekip gerekmediği "Quick start"ta iki seçenekle anlatılıyordu ama ilk ekranda değildi. Şimdi tek satır kurulum komutu ve "ne gerekir" dürüstçe ilk ekranda.
- "Current result: `60 passed, 3 skipped`" doğruydu (Linux CI), Windows sayısı yoktu; şimdi ikisi de yazılı (69/3 Linux CI'den, 66/6 Windows'ta ölçüldü).
- Sağlık kontrolü komutu (`python -c ...`) yerine `voice-io-mcp --check`.
- "A tool nobody runs stays right by default" gibi bölümler ve mimari anlatımı olduğu gibi bırakıldı; sayıları ("4 tools", "200 characters", "25MB", "4000 characters") koddan doğrulandı.
- `assets/banner.svg` hesabın banner üreticisinden; elle değiştirilmedi. `assets/fallback-chain.svg` ve `scope.svg` diyagram, gerçek çıktı iddiası taşımıyor.

## Testler ve CI

Önce: Linux CI `60 passed, 3 skipped`; Windows'ta `56 passed, 1 failed, 6 skipped`. Sonra: Linux CI (dal, `40c8696`) `69 passed, 3 skipped` (3.11 ve 3.13), `yerel` işi `3 passed`, `paket` ve `mcp-vet` işleri yeşil; Windows `66 passed, 6 skipped`. +9 yeni test (`tests/test_cli.py`).

## Günlük "Ekosistem denetimi" (#19, profil deposu)

Bu depoya ait açık bulgular yalnızca `project-meta.json` (60 test) ↔ `meta-source.json` (35) ve `summary` ↔ depo `description` ayrışması. Furki'nin kararı beklendiği için (`/meta` birleşmiş içeriği geri alır) **kapatılmadı**.

## Çözülmeyenler

- Groq'un canlı uçları ve yerel modellerin bu makinedeki çalışması ölçülmedi (yukarıda).
- `serverInfo` sürüm alanı boş (SDK).
- GitHub depo `description`'ı ve `meta-source.json` bayat; Furki'nin kararı.
