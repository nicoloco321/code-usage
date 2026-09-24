#!/usr/bin/env python3
"""Build the planes screen's aircraft art from Norebbo's side-view illustrations.

For each airline livery and blank (all-white) aircraft below, this downloads
Norebbo's illustration, cuts out the gear-up plane (the top one of the pair)
onto a transparent background, and writes it as a PNG plus an index.json the
display reads:

    pip install pillow numpy scipy          # on your PC - too heavy for a Pi 2
    python3 pi/build_liveries.py out/liveries
    scp -r out/liveries nico@claude-display.local:.local/share/claude-display/

The illustrations are (c) Norebbo (norebbo.com), shared for personal use:
keep the PNGs on your own display - don't commit or redistribute them (the
output folder is gitignored). Downloads are cached in <out>/.cache, so a
re-run only fetches what's new.

The display picks the art in this order (see plane_art_for in claude_display.py):
the airline's livery on this exact type, else the blank of the type, else the
planespotters photo.
"""
import argparse, json, os, sys, time, urllib.request

UPLOADS = "https://www.norebbo.com/wp-content/uploads/"
USER_AGENT = "claude-usage-display/1.0 (+https://github.com/nicoloco321/code-usage)"
WIDTH = 640  # px; the bar layout shows it about 430 wide

# (livery, ICAO type) -> illustration. Livery is the callsign's airline code,
# or a regional brand (united-express, delta-connection, american-eagle) -
# see REGIONAL_BRANDS in claude_display.py. Current liveries only (plus older
# ones still flying on a lot of the fleet, like United's 2010 globe).
LIVERIES = {
    ("AMX", "B737"): "2016/04/737-700_aeromexico.jpg",
    ("AMX", "B788"): "2016/04/787-8_aeromexico.jpg",
    ("ACA", "A321"): "2025/09/A321_air_canada_sharklets.jpg",
    ("ACA", "B38M"): "2025/09/737-8_MAX_air_canada.jpg",
    ("ACA", "B789"): "2025/09/787-9_air_canada.jpg",
    ("AIC", "B788"): "2022/06/787-8_air-india_livery.jpg",
    ("ASA", "B738"): "2020/04/737-800_alaska_airlines_new_livery_white.jpg",
    ("ASA", "B739"): "2018/05/737-900_Alaska_Airlines_2015_updated_livery.jpg",
    ("ASA", "B39M"): "2014/01/737-9_MAX_alaska.jpg",
    ("ASA", "E75L"): "2020/04/ERJ-175_alaska_airlines_new_livery.jpg",
    ("ANA", "A320"): "2013/02/A320_ANA_livery.jpg",
    ("ANA", "B77W"): "2013/02/777-300_ANA_livery.jpg",
    ("ANA", "B788"): "2013/02/787-8_ANA_livery.jpg",
    ("AAY", "A319"): "2015/09/A319_allegiant_air_side_profile.jpg",
    ("AAL", "A319"): "2013/01/A319_american_airlines.jpg",
    ("AAL", "A321"): "2016/04/A321T_American_side_view.jpg",
    ("AAL", "B738"): "2014/12/737-800_american_airlines_new_colors.jpg",
    ("AAL", "B752"): "2018/05/757-200_american_airlines_white_background.jpg",
    ("AAL", "B772"): "2014/10/777-200LR_american_airlines_new_livery.jpg",
    ("AAL", "B77W"): "2013/01/777-300_american_airlines_new_livery.jpg",
    ("AAL", "B788"): "2013/01/787-8_american_airlines.jpg",
    ("american-eagle", "E75L"): "2015/12/ERJ-175_american_eagle_1024.jpg",
    ("MXY", "E195"): "2021/11/breeze_airways_livery_white_background.jpg",
    ("BAW", "A35K"): "2015/02/A350-1000_british_airways_livery-1.jpg",
    ("BAW", "A388"): "2013/06/A380-800_british_airways.jpg",
    ("BAW", "B772"): "2015/02/777-200_british_airways.jpg",
    ("DAL", "A321"): "2014/10/A321_Delta.jpg",
    ("DAL", "A21N"): "2014/10/A321neo_delta_side_view.jpg",
    ("DAL", "A333"): "2014/10/A330-300_delta.jpg",
    ("DAL", "A339"): "2014/10/A330-900neo_delta_livery.jpg",
    ("DAL", "A359"): "2014/10/A350-900_delta_air_lines.jpg",
    ("DAL", "B738"): "2014/10/737-800_delta.jpg",
    ("DAL", "B752"): "2014/10/757-200_delta_onwards_and_upwards_livery_with_winglets.jpg",
    ("DAL", "B753"): "2014/10/757-300_delta.jpg",
    ("delta-connection", "E75L"): "2014/10/ERJ-175_delta_connection.jpg",
    ("UAE", "A388"): "2023/11/A380-800_emirates_2023_livery.jpg",
    ("UAE", "B77L"): "2012/12/777-200LR_illustration_emirates_white_background.jpg",
    ("ETD", "B789"): "2020/11/etihad-new-livery-white-background.jpg",
    ("HAL", "A21N"): "2014/12/A321_NEO_hawaiian.jpg",
    ("HAL", "A332"): "2024/03/A330-200_hawaiian_airlines_2017_livery.jpg",
    ("HAL", "B712"): "2014/12/717-200_hawaiian.jpg",
    ("HAL", "B789"): "2024/07/787-9_hawaiian_airlines.jpg",
    ("JAL", "B738"): "2022/05/737-800_JAL_tsurumaru_livery.jpg",
    ("JAL", "B788"): "2022/05/787-8_japan_airlines_tsurumaru_livery.jpg",
    ("KLM", "B738"): "2022/01/737-800_KLM_new_livery.jpg",
    ("KLM", "B772"): "2022/01/777-200_klm_asia_new_livery.jpg",
    ("KAL", "A333"): "2022/08/A330-300_korean_air.jpg",
    ("KAL", "A388"): "2022/08/A380-800_korean_air.jpg",
    ("KAL", "B748"): "2022/08/747-8i_korean_air.jpg",
    ("KAL", "B77W"): "2024/07/777-300_korean_air.jpg",
    ("KAL", "B78X"): "2025/03/787-10_korean_air_new_livery.jpg",
    ("KAL", "BCS3"): "2022/08/A220-300_korean_air.jpg",
    ("DLH", "A359"): "2020/10/lufthansa-livery-a350.jpg",
    ("DLH", "A388"): "2013/06/A380-800_lufthansa.jpg",
    ("QTR", "A359"): "2022/08/A350-900_qatar_airways_livery.jpg",
    ("QTR", "A388"): "2022/08/A380-800_qatar_airways_livery.jpg",
    ("QTR", "B77L"): "2022/08/777F_qatar_airways_cargo_livery.jpg",
    ("QTR", "B77W"): "2022/08/777-300ER_qatar_airways_livery.jpg",
    ("SWA", "B738"): "2020/12/737-800_southwest.jpg",
    ("NKS", "A319"): "2015/01/A319_Spirit_yellow.jpg",
    ("SCX", "B738"): "2022/08/737-800_sun_country_2018_livery.jpg",
    ("UAL", "A319"): "2020/07/A319_united_new_livery.jpg",
    ("UAL", "A320"): "2020/07/A320_united_airlines_blue_livery_side_view.jpg",
    ("UAL", "A21N"): "2024/03/A321_NEO_united.jpg",
    ("UAL", "B738"): "2020/07/new_united_livery_737.jpg",
    ("UAL", "B739"): "2014/04/737-900ER_united_airlines.jpg",
    ("UAL", "B39M"): "2024/03/737-9_MAX_united_2019_livery.jpg",
    ("UAL", "B752"): "2014/04/757-200_united_airlines_with_winglets.jpg",
    ("UAL", "B763"): "2020/07/767-300_united_airlines_new_livery.jpg",
    ("UAL", "B772"): "2020/07/777-200_united_airlines_2019_livery.jpg",
    ("UAL", "B77W"): "2014/04/777-300_united_airlines.jpg",
    ("UAL", "B788"): "2014/10/787-8_united_airlines_illustration.jpg",
    ("united-express", "CRJ2"): "2024/03/CRJ-200_united_express_2019_livery.jpg",
    ("united-express", "E75L"): "2014/04/ERJ-175_united_express_livery.jpg",
    ("UPS", "A306"): "2024/08/A300-600F_ups_2003_livery.jpg",
    ("UPS", "B748"): "2024/08/747-8F_ups_2014_livery.jpg",
    ("UPS", "B752"): "2024/08/757-200PF_ups_2014_livery.jpg",
    ("UPS", "B763"): "2024/08/767-300F_ups_2003_livery.jpg",
    ("UPS", "MD11"): "2024/08/MD-11F_ups_2014_livery.jpg",
}

# ICAO type -> the all-white illustration of it (airliners, props, bizjets).
BLANKS = {
    "A318": "2018/01/A318_cfm56_white_sm.jpg",
    "A319": "2015/01/airbus_a319_white_cm56_engines.jpg",
    "A19N": "2017/09/A319_NEO_CFM_LEAP_white_sm.jpg",
    "A320": "2015/01/a320_white_with_sharklet.jpg",
    "A20N": "2017/08/A320_NEO_CFM_LEAP_white_sm.jpg",
    "A321": "2015/01/airbus_a321_white_cm56_engines_sharklets.jpg",
    "A21N": "2017/09/A321_NEO_CFM_LEAP_white_sm.jpg",
    "A306": "2024/08/A300-600F_all_white.jpg",
    "A310": "2015/07/A310-300_white.jpg",
    "A332": "2016/02/A330-200_GE_white.jpg",
    "A333": "2016/02/A330-300_GE_white.jpg",
    "A338": "2018/06/A330-800_NEO_white_sm.jpg",
    "A339": "2018/06/A330-900_NEO_white_sm.jpg",
    "A342": "2019/01/A340-200_white.jpg",
    "A343": "2016/04/A340-300_white.jpg",
    "A345": "2016/08/A340-500_white_sm.jpg",
    "A346": "2016/11/A340-600_white_sm.jpg",
    "A359": "2013/07/A350-900_white.jpg",
    "A35K": "2015/11/A350-1000_white.jpg",
    "A388": "2013/06/A380-800_white.jpg",
    "BCS1": "2016/02/CS100_white.jpg",
    "BCS3": "2016/02/CS300_white.jpg",
    "B712": "2017/06/717-200_white_sm.jpg",
    "B733": "2018/09/737-300_white_sm.jpg",
    "B734": "2018/09/737-400_white_sm.jpg",
    "B735": "2018/09/737-500_white_blended_winglets_sm.jpg",
    "B736": "2018/09/737-600_white_sm.jpg",
    "B737": "2015/01/737-700_white.jpg",
    "B738": "2012/11/737-800_white_winglets.jpg",
    "B739": "2016/07/737-900er_white_split_scimitar_sm.jpg",
    "B37M": "2016/07/737_Max_7_white.jpg",
    "B38M": "2016/07/737_Max_8_white_sm.jpg",
    "B39M": "2018/05/737-9_MAX_white_sm.jpg",
    "B3XM": "2019/01/737-10_MAX_white.jpg",
    "B752": "2015/01/757-200_white_winglets.jpg",
    "B753": "2017/03/757-300_white_winglets_sm.jpg",
    "B762": "2015/01/767-200_white.jpg",
    "B763": "2015/01/767-300_winglets_white.jpg",
    "B764": "2015/01/767-400_white.jpg",
    "B772": "2012/12/777-200_white.jpg",
    "B77L": "2017/10/777F_white_sm.jpg",           # mostly 777Fs over the US
    "B773": "2015/01/777-300_white.jpg",
    "B77W": "2015/01/777-300_white.jpg",
    "B778": "2019/12/777-8_white.jpg",
    "B779": "2019/12/777-9_white.jpg",
    "B788": "2013/02/787-8_white.jpg",
    "B789": "2015/01/787-9_white.jpg",
    "B78X": "2017/06/787-10_white_sm.jpg",
    "B742": "2019/08/747-200_white_GE_painted_engines.jpg",
    "B744": "2018/01/747-400F_white_sm.jpg",        # mostly freighters now
    "B748": "2015/12/747-8i_white.jpg",
    "MD11": "2024/08/MD-11F_all_white.jpg",
    "MD82": "2015/02/MD-80_white.jpg",
    "MD83": "2015/02/MD-80_white.jpg",
    "MD88": "2015/02/MD-80_white.jpg",
    "MD87": "2019/06/MD-87_white.jpg",
    "MD90": "2018/02/MD-90_white_sm.jpg",
    "DC10": "2015/01/DC-10-30_white.jpg",
    "MD10": "2018/11/DC-10-30F_MD-10_white.jpg",
    "DC93": "2020/06/DC-9-30_white.jpg",
    "DC94": "2020/05/DC-9-40_white.jpg",
    "DC95": "2020/05/DC-9-50_white.jpg",
    "DC87": "2023/01/DC-8-73CF_white.jpg",
    "E135": "2018/05/ERJ-135_white.jpg",
    "E35L": "2018/05/ERJ-135_white.jpg",
    "E145": "2018/04/ERJ-145_white.jpg",
    "E45X": "2018/04/ERJ-145XR_white_sm.jpg",
    "E75L": "2015/10/ERJ-175_white_1024.jpg",
    "E75S": "2015/10/ERJ-175_white_1024.jpg",
    "E190": "2015/06/ERJ-190_white.jpg",
    "E195": "2019/06/ERJ-195_white.jpg",
    "E290": "2019/03/ERJ-190-E2_white.jpg",
    "E295": "2019/03/ERJ-195-E2_white.jpg",
    "E120": "2015/02/EMB-120_white.jpg",
    "CRJ1": "2015/04/CRJ-200_white_small.jpg",
    "CRJ2": "2015/04/CRJ-200_white_small.jpg",
    "CRJ7": "2015/05/CRJ-700_template_white.jpg",
    "CRJ9": "2016/07/CRJ-900_white_sm.jpg",
    "CRJX": "2019/06/CRJ-1000_white.jpg",
    "DH8B": "2018/01/DHC-8-200_white_sm.jpg",
    "DH8C": "2018/05/DHC-8-300_white.jpg",
    "DH8D": "2015/08/Q400_white.jpg",
    "AT43": "2018/06/ATR_42_white_sm.jpg",
    "AT45": "2018/06/ATR_42_white_sm.jpg",
    "AT46": "2018/06/ATR_42_white_sm.jpg",
    "AT72": "2017/04/ATR_72_white_sm.jpg",
    "AT75": "2017/04/ATR_72_white_sm.jpg",
    "AT76": "2017/04/ATR_72_white_sm.jpg",
    "SF34": "2018/12/Saab_340B_white.jpg",
    "B190": "2022/07/Beechcraft-1900D-white.jpg",
    "J328": "2019/01/Dornier_328JET_white.jpg",
    "D328": "2019/01/Dornier_328-110_white_sm.jpg",
    "JS41": "2024/05/Jetstream-41_white.jpg",
    "F70": "2024/07/Fokker_70_white.jpg",
    "F100": "2024/07/Fokker_100_white.jpg",
    "B462": "2018/11/BAe_146-200_Avro_RJ85_white.jpg",
    "RJ85": "2018/11/BAe_146-200_Avro_RJ85_white.jpg",
    "SH36": "2026/01/short_360_white.jpg",
    "C17": "2023/05/C-17_white.jpg",
    # private and business
    "GLF5": "2026/09/Gulfstream_G550_white.jpg",
    "GLF6": "2021/01/Gulfstream_G650ER_white.jpg",
    "GL5T": "2022/05/Global-5000_white.jpg",
    "GL7T": "2021/04/Global_7500_white.jpg",
    "LJ45": "2021/08/Learjet-45-white.jpg",
    "LJ60": "2025/03/Learjet-60-white.jpg",
    "FA50": "2021/06/dassault-falcon-50_winglets_white.jpg",
    "C750": "2020/12/Cessna_Citation_X_with_winglets_white.jpg",
    "BE20": "2020/11/Beechcraft_King_Air_B200_white.jpg",
    "C208": "2017/06/cessna_208_grand_caravan_white_sm.jpg",
}


def fetch(path, cache):
    local = os.path.join(cache, path.replace("/", "_"))
    if not os.path.exists(local):
        req = urllib.request.Request(UPLOADS + path, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(local, "wb") as f:
            f.write(data)
        time.sleep(0.8)  # go easy on the site
    return local


def gear_up_plane(path):
    """The top plane of Norebbo's gear-up / gear-down pair, as an RGBA
    image cropped to the plane, background made transparent."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    rgb = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    h, w, _ = rgb.shape
    lo = rgb.min(axis=2)
    ink = lo < 240
    # The gear-down plane is the gear-up one again, straight below it: find
    # that offset (where the drawing best overlaps itself shifted down)...
    small = ink[::2, ::2]
    best, dy = 0.0, 0
    for d in range(small.shape[0] // 5, small.shape[0] * 4 // 5):
        overlap = np.count_nonzero(small[:-d] & small[d:]) / max(1, np.count_nonzero(small[:-d]))
        if overlap > best:
            best, dy = overlap, d * 2
    if best > 0.6:
        # ...then the top plane is whatever has its copy that far below
        # (checked with a little slack for JPEG noise). A tail reaching up
        # into the plane above, or a caption, has no copy, so stays out.
        below = np.zeros_like(ink)
        below[:-dy] = ndimage.binary_dilation(ink, iterations=2)[dy:]
        region = ink & below
    else:  # just the one plane
        region = ink.copy()
    lab, n = ndimage.label(ndimage.binary_closing(region, np.ones((5, 5))))
    if not n:
        raise ValueError("no plane found")
    sizes = ndimage.sum(region, lab, range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    near_main = ndimage.binary_dilation(lab == main, iterations=15)
    keep_ids = [i + 1 for i in range(n) if sizes[i] >= 30 or near_main[lab == i + 1].any()]
    region &= np.isin(lab, keep_ids)
    ink = ndimage.binary_closing(ink, np.ones((5, 5)))
    # the silhouette: that ink plus whatever it encloses (white paint)
    solid = ndimage.binary_fill_holes(ndimage.binary_closing(region, np.ones((3, 3))))
    bg_lab, _ = ndimage.label((lo >= 250) & ~solid)
    edge = set(np.unique(np.concatenate([bg_lab[0], bg_lab[-1], bg_lab[:, 0], bg_lab[:, -1]]))) - {0}
    keep = ~np.isin(bg_lab, list(edge))
    keep &= ~ndimage.binary_dilation(ink & ~region & ~solid, iterations=2)  # the other plane
    keep = ndimage.binary_fill_holes(keep) & ndimage.binary_dilation(solid, iterations=3)
    # soft edge: rim pixels are un-blended from the white they were drawn on
    alpha = keep.astype(np.float32)
    rim = keep & ndimage.binary_dilation(~keep, iterations=2)
    alpha[rim] = np.clip((255.0 - lo[rim]) / 55.0, 0, 1)
    a3 = np.maximum(alpha, 1e-3)[..., None]
    out = rgb.copy()
    out[rim] = np.clip(((rgb - (1 - a3) * 255.0) / a3)[rim], 0, 255)
    rgba = np.dstack([out, alpha * 255]).astype(np.uint8)
    ys, xs = np.nonzero(alpha > 0.02)
    img = Image.fromarray(rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1], "RGBA")
    if img.width > WIDTH:
        img = img.resize((WIDTH, round(img.height * WIDTH / img.width)), Image.LANCZOS)
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="output folder, e.g. out/liveries")
    ap.add_argument("--only", help="just these files (comma-separated source names), for testing")
    args = ap.parse_args()
    cache = os.path.join(args.out, ".cache")
    os.makedirs(cache, exist_ok=True)
    index = {"credit": "Norebbo", "liveries": {}, "blanks": {}}
    jobs = [(f"{liv}_{t}.png", src, ("liveries", liv, t)) for (liv, t), src in LIVERIES.items()]
    jobs += [(f"blank_{t}.png", src, ("blanks", None, t)) for t, src in BLANKS.items()]
    done = {}
    for name, src, (kind, liv, t) in jobs:
        if args.only and os.path.basename(src) not in args.only.split(","):
            continue
        try:
            if src not in done:
                gear_up_plane(fetch(src, cache)).save(os.path.join(args.out, name), optimize=True)
                done[src] = name
            elif done[src] != name:  # the same drawing serves two type codes
                with open(os.path.join(args.out, done[src]), "rb") as a, \
                        open(os.path.join(args.out, name), "wb") as b:
                    b.write(a.read())
        except Exception as e:
            print(f"skipped {name} ({src}): {e}", file=sys.stderr)
            continue
        entry = {"file": name, "source": UPLOADS + src}
        if kind == "liveries":
            index["liveries"].setdefault(liv, {})[t] = entry
        else:
            index["blanks"][t] = entry
        print(name)
    with open(os.path.join(args.out, "index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    print(f"{sum(map(len, index['liveries'].values()))} liveries, {len(index['blanks'])} blanks "
          f"-> {args.out}")


if __name__ == "__main__":
    main()
