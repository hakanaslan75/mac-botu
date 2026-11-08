"""Maç botu: ilk 11'leri, golleri ve maç sonuçlarını Telegram'a hazır tweet olarak yollar.

Veri: ESPN'in herkese açık skor verisi (anahtar gerekmez, ücretsiz).
Telegram mesajındaki "X'te paylaş" düğmesine basınca X, metin yazılmış halde açılır.
Yalnızca Python'un kendi kütüphaneleri kullanılır; paket kurmak gerekmez.

Kullanım:
  python bot.py --kuru                      # hiçbir şey göndermeden ekrana yazar
  python bot.py --kuru --tarih 20261108 --hepsi   # o günün tüm maçlarını dener
  python bot.py --chat-id-bul               # Telegram sohbet numarasını bulur
  python bot.py --test-mesaji               # Telegram'a deneme mesajı yollar
  python bot.py --dongu 25                  # canlı maç varken 25 dakika boyunca dakikada bir bakar
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KLASOR = os.path.dirname(os.path.abspath(__file__))
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
TR_SAATI = timezone(timedelta(hours=3))
AYLAR = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
ETIKETLER = {
    "tur.1": "#SüperLig",
    "uefa.champions": "#ŞampiyonlarLigi",
    "uefa.europa": "#AvrupaLigi",
    "uefa.europa.conf": "#KonferansLigi",
    "eng.1": "#PremierLig",
    "esp.1": "#LaLiga",
    "ita.1": "#SerieA",
    "ger.1": "#Bundesliga",
    "fra.1": "#Ligue1",
}
ILK11_ONCE_DK = 90      # maçtan en çok bu kadar dakika önce ilk 11'e bak
ILK11_SONRA_DK = 20     # başladıktan sonra en çok bu kadar dakika ilk 11 gönder
SONUC_SURESI_SAAT = 6   # başlama saatinden bu kadar saat sonrasına kadar gol/sonuç gönder


# ----------------------------------------------------------------- ayarlar

def env_oku():
    yol = os.path.join(KLASOR, ".env")
    if not os.path.exists(yol):
        return
    with open(yol, encoding="utf-8-sig") as f:
        for satir in f:
            satir = satir.strip()
            if not satir or satir.startswith("#") or "=" not in satir:
                continue
            ad, deger = satir.split("=", 1)
            ad, deger = ad.strip(), deger.strip().strip('"').strip("'")
            if ad and not os.environ.get(ad):
                os.environ[ad] = deger


def liste(ad, varsayilan=""):
    deger = os.environ.get(ad, "").strip() or varsayilan
    return [x.strip() for x in deger.split(",") if x.strip()]


def isimleri_oku():
    yol = os.path.join(KLASOR, "isim_duzeltme.json")
    try:
        with open(yol, encoding="utf-8") as f:
            veri = json.load(f)
    except (OSError, ValueError):
        veri = {}
    return veri.get("takimlar", {}), veri.get("oyuncular", {})


TAKIM_ADI, OYUNCU_ADI = isimleri_oku()


def takim(ad):
    return TAKIM_ADI.get(ad, ad)


def oyuncu(ad):
    return OYUNCU_ADI.get(ad, ad)


# ----------------------------------------------------------------- yardımcılar

def json_al(url, deneme=3):
    for i in range(deneme):
        try:
            # Not: ESPN, kimliğinde "bot" geçen istekleri reddediyor; Python'un standart kimliği kullanılır.
            with urllib.request.urlopen(url, timeout=30) as yanit:
                return json.load(yanit)
        except (urllib.error.URLError, TimeoutError, ValueError) as hata:
            if i == deneme - 1:
                raise
            print(f"  ! tekrar denenecek ({hata})")
            time.sleep(3)


def x_uzunlugu(metin):
    """X'in karakter sayımı: Latin/Türkçe harfler 1, emoji ve bazı işaretler 2 sayılır."""
    toplam = 0
    for h in metin:
        k = ord(h)
        if k <= 4351 or 8192 <= k <= 8205 or 8208 <= k <= 8223 or 8242 <= k <= 8247:
            toplam += 1
        else:
            toplam += 2
    return toplam


LINK_DESENI = re.compile(r"https?://|www\.|\b[\w-]+\.(com|net|org|tr|io|co|tv|me|ly)\b", re.I)


def zaman(e):
    return datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(timezone.utc)


def saat_yazisi(dt):
    t = dt.astimezone(TR_SAATI)
    return f"{t.day} {AYLAR[t.month]}, {t:%H:%M}"


def taraflar(e):
    c = e["competitions"][0]["competitors"]
    ev = next(x for x in c if x["homeAway"] == "home")
    dep = next(x for x in c if x["homeAway"] == "away")
    return ev, dep


def mac_adi(e):
    ev, dep = taraflar(e)
    return f"{takim(ev['team']['displayName'])} - {takim(dep['team']['displayName'])}"


# ----------------------------------------------------------------- tweet metinleri

def mevki_sirasi(kisaltma):
    """(hat, derinlik, yan). Hat: 0 kaleci, 1 defans, 2 orta saha, 3 forvet. Yan: sağdan sola."""
    k = (kisaltma or "").upper()
    if k.endswith("-R"):
        yan = 1                  # sağ stoper / sağ iç
    elif k.endswith("-L"):
        yan = 3
    elif k.startswith("R"):
        yan = 0                  # sağ bek / sağ kanat
    elif k.startswith("L"):
        yan = 4
    else:
        yan = 2                  # orta
    if k in ("G", "GK"):
        return 0, 0, yan
    if k.startswith("CD") or k in ("LB", "RB", "LWB", "RWB", "SW", "D", "CB"):
        return 1, 0, yan
    if k.startswith("DM"):
        return 2, 0, yan
    if k.startswith("CM") or k in ("LM", "RM", "M"):
        return 2, 1, yan
    if k.startswith("AM"):
        return 2, 2, yan
    if k.startswith("F") or k.startswith("CF") or k in ("ST", "LW", "RW", "LF", "RF", "SS"):
        return 3, 0, yan
    return 2, 1, yan


HAT_ADI = ["Kaleci", "Defans", "Orta saha", "Forvet"]


def ilk11_metni(e, kadro, etiket):
    ilk = [p for p in kadro["roster"] if p.get("starter")]
    ilk.sort(key=lambda p: mevki_sirasi(p.get("position", {}).get("abbreviation")))
    hat_no, uzun, kisa = [], [], []
    for p in ilk:
        a = p["athlete"]
        tam = a.get("displayName") or a.get("fullName") or "?"
        ad = oyuncu(tam)
        uzun.append(ad)
        if " " not in ad:
            kisa.append(ad)
        elif ad != tam:
            kisa.append(ad.split()[-1])          # düzeltilmiş isim: son kelime
        else:
            kisa.append(a.get("lastName") or ad.split()[-1])
        hat_no.append(mevki_sirasi(p.get("position", {}).get("abbreviation"))[0])

    dizilis = kadro.get("formation") or ""
    try:
        sayilar = [int(x) for x in dizilis.split("-")]
    except ValueError:
        sayilar = []
    adet = [hat_no.count(i) for i in range(4)]
    dizilise_gore = (len(sayilar) >= 2 and sum(sayilar) == 10 and adet[0] == 1
                     and sayilar[0] == adet[1] and sayilar[-1] == adet[3]
                     and sum(sayilar[1:-1]) == adet[2])

    def yaz(kisalanlar):
        adlar = [kisa[i] if i in kisalanlar else uzun[i] for i in range(len(uzun))]
        if dizilise_gore:
            # 4-2-3-1 gibi: her hat ayrı satır, kaleciden forvete
            satir, bas = [], 0
            for n in [1] + sayilar:
                satir.append(", ".join(adlar[bas:bas + n]))
                bas += n
        else:
            satir = [f"{HAT_ADI[h]}: {', '.join(a for a, hh in zip(adlar, hat_no) if hh == h)}"
                     for h in range(4) if h in hat_no]
        bas_satir = f"📋 {takim(kadro['team']['displayName'])} ilk 11" + (f" ({dizilis})" if dizilis else "")
        metin = [bas_satir, f"{mac_adi(e)} | {saat_yazisi(zaman(e))}", ""] + satir
        if etiket:
            metin += ["", etiket]
        return "\n".join(metin)

    # 280'i geçerse en uzun isimden başlayarak soyadına indir
    kisalanlar = set()
    metin = yaz(kisalanlar)
    for i in sorted(range(len(uzun)), key=lambda i: -len(uzun[i])):
        if x_uzunlugu(metin) <= 280:
            break
        if kisa[i] != uzun[i]:
            kisalanlar.add(i)
            metin = yaz(kisalanlar)
    return metin


def goller(e):
    c = e["competitions"][0]
    ev, dep = taraflar(e)
    liste_ = []
    for d in c.get("details", []):
        if d.get("scoringPlay") and not d.get("shootout"):
            liste_.append(d)
    liste_.sort(key=lambda d: d.get("clock", {}).get("value", 0))
    sonuc = []
    skor = {ev["id"]: 0, dep["id"]: 0}
    for d in liste_:
        tid = d.get("team", {}).get("id")
        if tid in skor:
            skor[tid] += 1
        kisi = (d.get("athletesInvolved") or [{}])[0]
        sonuc.append({
            "anahtar": f"gol:{e['id']}:{tid}:{kisi.get('id', '?')}:{d.get('clock', {}).get('displayValue', '?')}",
            "dakika": d.get("clock", {}).get("displayValue", "?"),
            "takim_id": tid,
            "oyuncu": oyuncu(kisi.get("displayName") or "?"),
            "kisa": oyuncu(kisi.get("shortName") or kisi.get("displayName") or "?"),
            "penalti": bool(d.get("penaltyKick")),
            "kk": bool(d.get("ownGoal")),
            "ev": skor[ev["id"]],
            "dep": skor[dep["id"]],
        })
    return sonuc


def gol_metni(e, g, etiket):
    ev, dep = taraflar(e)
    atan = takim((ev if g["takim_id"] == ev["id"] else dep)["team"]["displayName"])
    not_ = " (penaltı)" if g["penalti"] else (" (kendi kalesine)" if g["kk"] else "")
    satirlar = [
        f"⚽ GOL! {atan}",
        f"{g['dakika']} {g['oyuncu']}{not_}",
        "",
        f"{takim(ev['team']['displayName'])} {g['ev']}-{g['dep']} {takim(dep['team']['displayName'])}",
    ]
    if etiket:
        satirlar += [etiket]
    return "\n".join(satirlar)


def sonuc_metni(e, etiket, kisa=False):
    ev, dep = taraflar(e)
    satirlar = ["🏁 MAÇ SONU",
                f"{takim(ev['team']['displayName'])} {ev.get('score', '0')}-{dep.get('score', '0')} "
                f"{takim(dep['team']['displayName'])}"]
    if ev.get("shootoutScore") is not None and dep.get("shootoutScore") is not None:
        satirlar.append(f"Penaltılar: {ev['shootoutScore']}-{dep['shootoutScore']}")
    gl = goller(e)
    if gl:
        satirlar.append("")
    for taraf in (ev, dep):
        kendi = [g for g in gl if g["takim_id"] == taraf["id"]]
        if not kendi:
            continue
        sirali = {}
        for g in kendi:
            ad = g["kisa"] if kisa else g["oyuncu"]
            ek = " (pen.)" if g["penalti"] else (" (k.k.)" if g["kk"] else "")
            sirali.setdefault(ad, []).append(g["dakika"] + ek)
        parca = "; ".join(f"{ad} {', '.join(dk)}" for ad, dk in sirali.items())
        satirlar.append(f"⚽ {takim(taraf['team']['displayName'])}: {parca}")
    if etiket:
        satirlar += ["", etiket]
    return "\n".join(satirlar)


def sigdir(uret):
    """Önce uzun isimlerle dener, 280'i geçerse kısa isimlerle yazar."""
    metin = uret(False)
    if x_uzunlugu(metin) > 280:
        metin = uret(True)
    return metin


# ----------------------------------------------------------------- Telegram

def telegram(metot, veri):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN yok (.env dosyasına ya da GitHub Secrets'a yaz)")
    istek = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{metot}",
        data=json.dumps(veri).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(istek, timeout=30) as yanit:
            return json.load(yanit)
    except urllib.error.HTTPError as hata:
        govde = hata.read().decode("utf-8", "replace")
        if hata.code == 429:
            try:
                bekle = int(json.loads(govde).get("parameters", {}).get("retry_after", 5))
            except (ValueError, AttributeError):
                bekle = 5
            time.sleep(bekle + 1)
            return telegram(metot, veri)
        # Hata mesajında token görünmesin
        raise RuntimeError(f"Telegram hatası {hata.code}: {govde}") from None


def paylas_linki(metin):
    return "https://x.com/intent/post?text=" + urllib.parse.quote(metin, safe="")


def gonder(metin, kuru):
    if LINK_DESENI.search(metin):
        print("  ! metinde link var gibi görünüyor, gönderilmedi:\n" + metin)
        return False
    uzunluk = x_uzunlugu(metin)
    if kuru:
        print("-" * 50)
        print(metin)
        print(f"[X uzunluğu: {uzunluk}/280 | paylaş linki: {len(paylas_linki(metin))} karakter]")
        return True
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID yok (python bot.py --chat-id-bul ile bul)")
    dugme = {"inline_keyboard": [[{"text": "📤 X'te paylaş", "url": paylas_linki(metin)}]]}
    ek = "" if uzunluk <= 280 else f"\n\n⚠️ {uzunluk} karakter, X sınırı 280. Göndermeden önce kısalt."
    telegram("sendMessage", {
        "chat_id": chat,
        "text": metin + ek,
        "reply_markup": dugme,
        "link_preview_options": {"is_disabled": True},
    })
    time.sleep(1)
    return True


# ----------------------------------------------------------------- durum kaydı

def durum_oku(yol):
    try:
        with open(yol, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("gonderilenler", {})
    return d


def durum_yaz(yol, d):
    sinir = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    d["gonderilenler"] = {k: v for k, v in d["gonderilenler"].items() if v >= sinir}
    d["son_kontrol"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    gecici = yol + ".tmp"
    with open(gecici, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(gecici, yol)


# ----------------------------------------------------------------- ana döngü

def mac_takip_ediliyor_mu(e, takimlar):
    if not takimlar:
        return True
    ev, dep = taraflar(e)
    adlar = {ev["team"]["displayName"].lower(), dep["team"]["displayName"].lower(),
             takim(ev["team"]["displayName"]).lower(), takim(dep["team"]["displayName"]).lower()}
    return any(t.lower() in ad for t in takimlar for ad in adlar)


def gunleri_bul(tarih):
    if tarih:
        return [tarih]
    simdi = datetime.now(timezone.utc)
    return [(simdi - timedelta(days=1)).strftime("%Y%m%d"), simdi.strftime("%Y%m%d")]


def tur(durum, kuru, hepsi, tarih):
    """Bir kez bakar. Canlı (ya da birazdan başlayacak) maç varsa True döner."""
    gonderilen = durum["gonderilenler"]
    simdi = datetime.now(timezone.utc)
    aktif = False
    hata_sayisi = istek_sayisi = 0
    takimlar = liste("TAKIMLAR")

    def isaretle(anahtar):
        gonderilen[anahtar] = datetime.now(timezone.utc).isoformat()

    for lig in liste("LIGLER", "tur.1"):
        etiket = os.environ.get("ETIKET", "").strip() or ETIKETLER.get(lig, "")
        maclar = {}
        for gun in gunleri_bul(tarih):
            istek_sayisi += 1
            try:
                veri = json_al(f"{ESPN}/{lig}/scoreboard?dates={gun}")
            except Exception as hata:
                hata_sayisi += 1
                print(f"! {lig} {gun} alınamadı: {hata}")
                continue
            for e in veri.get("events", []):
                maclar[e["id"]] = e

        for e in sorted(maclar.values(), key=lambda m: m["date"]):
            if not mac_takip_ediliyor_mu(e, takimlar):
                continue
            durum_tipi = e["status"]["type"]
            asama = durum_tipi.get("state")          # pre / in / post
            basla = zaman(e)
            dk_kaldi = (basla - simdi).total_seconds() / 60
            if asama == "in" or (asama == "pre" and -180 <= dk_kaldi <= 15):
                aktif = True

            # --- İlk 11
            ilk11_zamani = hepsi or (asama != "post" and -ILK11_SONRA_DK <= dk_kaldi <= ILK11_ONCE_DK)
            ilk11_anahtar = f"ilk11:{e['id']}"
            if ilk11_zamani and ilk11_anahtar not in gonderilen:
                istek_sayisi += 1
                try:
                    ozet = json_al(f"{ESPN}/{lig}/summary?event={e['id']}")
                except Exception as hata:
                    hata_sayisi += 1
                    print(f"! {mac_adi(e)} özeti alınamadı: {hata}")
                    ozet = {}
                kadrolar = ozet.get("rosters", [])
                hazir = len(kadrolar) == 2 and all(
                    sum(1 for p in k.get("roster", []) if p.get("starter")) == 11 for k in kadrolar)
                if hazir:
                    kadrolar.sort(key=lambda k: 0 if k.get("homeAway") == "home" else 1)
                    tamam = True
                    for k in kadrolar:
                        anahtar = f"{ilk11_anahtar}:{k['team']['id']}"
                        if anahtar in gonderilen:
                            continue
                        metin = ilk11_metni(e, k, etiket)
                        if gonder(metin, kuru):
                            isaretle(anahtar)
                        else:
                            tamam = False
                    if tamam:
                        isaretle(ilk11_anahtar)
                elif not hepsi:
                    print(f"  {mac_adi(e)}: ilk 11 henüz açıklanmadı ({int(dk_kaldi)} dk kaldı)")

            if asama == "pre":
                continue

            # --- Goller ve maç sonu: yalnızca yakın zamandaki maçlar
            if not hepsi and (simdi - basla) > timedelta(hours=SONUC_SURESI_SAAT):
                continue
            izlendi = f"izlendi:{e['id']}"
            ilk_kez_bitmis = asama == "post" and izlendi not in gonderilen and not hepsi
            for g in goller(e):
                if g["anahtar"] in gonderilen:
                    continue
                if ilk_kez_bitmis:
                    isaretle(g["anahtar"])   # bot geç açıldı: eski golleri tek tek yollama
                    continue
                if gonder(gol_metni(e, g, etiket), kuru):
                    isaretle(g["anahtar"])
            isaretle(izlendi)

            son_anahtar = f"son:{e['id']}"
            if asama == "post" and durum_tipi.get("completed") and son_anahtar not in gonderilen:
                if gonder(sigdir(lambda kisa: sonuc_metni(e, etiket, kisa)), kuru):
                    isaretle(son_anahtar)

    if istek_sayisi and hata_sayisi == istek_sayisi:
        raise RuntimeError("Hiçbir veri alınamadı (internet ya da ESPN sorunu)")
    return aktif


def chat_id_bul():
    veri = telegram("getUpdates", {})
    bulunan = {}
    for u in veri.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        sohbet = m.get("chat")
        if sohbet:
            bulunan[sohbet["id"]] = sohbet.get("first_name") or sohbet.get("title") or ""
    if not bulunan:
        print("Hiç mesaj bulunamadı. Telegram'da botuna /start yaz, sonra bunu tekrar çalıştır.")
    for sid, ad in bulunan.items():
        print(f"TELEGRAM_CHAT_ID={sid}   ({ad})")


def main():
    # Windows konsolunda emoji ve Türkçe harfler bozulmasın
    for akis in (sys.stdout, sys.stderr):
        try:
            akis.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    env_oku()
    ap = argparse.ArgumentParser(description="Maç botu")
    ap.add_argument("--kuru", action="store_true", help="Göndermeden ekrana yaz")
    ap.add_argument("--tarih", help="Bu günün maçlarına bak (YYYYAAGG)")
    ap.add_argument("--hepsi", action="store_true", help="Zaman sınırlarını yok say (deneme için)")
    ap.add_argument("--durum", help="Durum dosyası yolu")
    ap.add_argument("--dongu", type=float, default=0, help="Canlı maç varken bu kadar dakika dakikada bir bak")
    ap.add_argument("--chat-id-bul", action="store_true")
    ap.add_argument("--test-mesaji", action="store_true")
    a = ap.parse_args()

    if a.chat_id_bul:
        return chat_id_bul()
    if a.test_mesaji:
        gonder("✅ Maç botu çalışıyor. Bu bir deneme mesajıdır.", kuru=False)
        print("Deneme mesajı gönderildi.")
        return

    yol = a.durum or os.path.join(KLASOR, "durum_kuru.json" if a.kuru else "durum.json")
    durum = durum_oku(yol)
    basla = time.time()
    while True:
        print(f"[{datetime.now(TR_SAATI):%H:%M:%S}] kontrol ediliyor...")
        try:
            aktif = tur(durum, a.kuru, a.hepsi, a.tarih)
        finally:
            durum_yaz(yol, durum)
        if not a.dongu or not aktif or time.time() - basla + 60 > a.dongu * 60:
            break
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as hata:
        print(f"HATA: {hata}")
        sys.exit(1)
