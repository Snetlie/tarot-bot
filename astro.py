"""Расчёт натальной карты на чистом Python, без внешних библиотек.

Положения планет считаются по упрощённым элементам орбит (метод П. Шлиттера) с главными возмущениями.
Точность: Солнце, Луна и планеты в пределах угловых минут, Плутон около градуса. Этого достаточно, чтобы верно
определить знак, дом и крупные аспекты. Дома считаются по Плацидусу, а выше широты ±66° и при сбое — равными домами.
"""
import math
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

SIGNS = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]
ELEMENT = ["огонь", "земля", "воздух", "вода"]  # знаки идут по кругу: огонь, земля, воздух, вода
MODALITY = ["кардинальный", "фиксированный", "мутабельный"]
PLANET_NAMES = {"sun": "Солнце", "moon": "Луна", "mercury": "Меркурий", "venus": "Венера", "mars": "Марс", "jupiter": "Юпитер",
                "saturn": "Сатурн", "uranus": "Уран", "neptune": "Нептун", "pluto": "Плутон", "node": "Северный узел"}
PLANETS = list(PLANET_NAMES)
ASPECTS = [("conjunction", "соединение", 0, 8), ("opposition", "оппозиция", 180, 8), ("trine", "трин", 120, 7),
           ("square", "квадрат", 90, 7), ("sextile", "секстиль", 60, 5)]

sin = lambda x: math.sin(math.radians(x))  # noqa: E731
cos = lambda x: math.cos(math.radians(x))  # noqa: E731
tan = lambda x: math.tan(math.radians(x))  # noqa: E731
atan2 = lambda y, x: math.degrees(math.atan2(y, x))  # noqa: E731
norm = lambda x: x % 360.0  # noqa: E731


def julian_day(dt_utc):
    return dt_utc.timestamp() / 86400.0 + 2440587.5


def _elements(body, d):
    """Элементы орбиты на день d (дней от 1999-12-31 0:00 UT): N, i, w, a, e, M."""
    t = {
        "sun": (0.0, 0.0, 282.9404 + 4.70935e-5 * d, 1.0, 0.016709 - 1.151e-9 * d, 356.0470 + 0.9856002585 * d),
        "moon": (125.1228 - 0.0529538083 * d, 5.1454, 318.0634 + 0.1643573223 * d, 60.2666, 0.054900, 115.3654 + 13.0649929509 * d),
        "mercury": (48.3313 + 3.24587e-5 * d, 7.0047 + 5.00e-8 * d, 29.1241 + 1.01444e-5 * d, 0.387098, 0.205635 + 5.59e-10 * d, 168.6562 + 4.0923344368 * d),
        "venus": (76.6799 + 2.46590e-5 * d, 3.3946 + 2.75e-8 * d, 54.8910 + 1.38374e-5 * d, 0.723330, 0.006773 - 1.302e-9 * d, 48.0052 + 1.6021302244 * d),
        "mars": (49.5574 + 2.11081e-5 * d, 1.8497 - 1.78e-8 * d, 286.5016 + 2.92961e-5 * d, 1.523688, 0.093405 + 2.516e-9 * d, 18.6021 + 0.5240207766 * d),
        "jupiter": (100.4542 + 2.76854e-5 * d, 1.3030 - 1.557e-7 * d, 273.8777 + 1.64505e-5 * d, 5.20256, 0.048498 + 4.469e-9 * d, 19.8950 + 0.0830853001 * d),
        "saturn": (113.6634 + 2.38980e-5 * d, 2.4886 - 1.081e-7 * d, 339.3939 + 2.97661e-5 * d, 9.55475, 0.055546 - 9.499e-9 * d, 316.9670 + 0.0334442282 * d),
        "uranus": (74.0005 + 1.3978e-5 * d, 0.7733 + 1.9e-8 * d, 96.6612 + 3.0565e-5 * d, 19.18171 - 1.55e-8 * d, 0.047318 + 7.45e-9 * d, 142.5905 + 0.011725806 * d),
        "neptune": (131.7806 + 3.0173e-5 * d, 1.7700 - 2.55e-7 * d, 272.8461 - 6.027e-6 * d, 30.05826 + 3.313e-8 * d, 0.008606 + 2.15e-9 * d, 260.2471 + 0.005995147 * d),
    }[body]
    N, i, w, a, e, M = t
    return N, i, w, a, e, norm(M)


def _kepler(M, e):
    """Эксцентрическая аномалия в градусах."""
    E = M + math.degrees(e) * sin(M) * (1 + e * cos(M))
    for _ in range(12):
        E -= (E - math.degrees(e) * sin(E) - M) / (1 - e * cos(E))
    return E


def _orbit(body, d):
    """Гелиоцентрические (для Луны геоцентрические) эклиптические x, y, z и угловые элементы."""
    N, i, w, a, e, M = _elements(body, d)
    E = _kepler(M, e)
    xv, yv = a * (cos(E) - e), a * math.sqrt(1 - e * e) * sin(E)
    v, r = atan2(yv, xv), math.hypot(xv, yv)
    vw = v + w
    xh = r * (cos(N) * cos(vw) - sin(N) * sin(vw) * cos(i))
    yh = r * (sin(N) * cos(vw) + cos(N) * sin(vw) * cos(i))
    zh = r * sin(vw) * sin(i)
    return xh, yh, zh, r


def _sun(d):
    N, i, w, a, e, M = _elements("sun", d)
    E = _kepler(M, e)
    xv, yv = cos(E) - e, math.sqrt(1 - e * e) * sin(E)
    lon, r = norm(atan2(yv, xv) + w), math.hypot(xv, yv)
    return lon, r


def _planet_helio(body, d):
    xh, yh, zh, r = _orbit(body, d)
    if body in ("jupiter", "saturn", "uranus"):
        Mj, Ms, Mu = _elements("jupiter", d)[5], _elements("saturn", d)[5], _elements("uranus", d)[5]
        lon, lat = atan2(yh, xh), atan2(zh, math.hypot(xh, yh))
        if body == "jupiter":
            lon += (-0.332 * sin(2 * Mj - 5 * Ms - 67.6) - 0.056 * sin(2 * Mj - 2 * Ms + 21) + 0.042 * sin(3 * Mj - 5 * Ms + 21)
                    - 0.036 * sin(Mj - 2 * Ms) + 0.022 * cos(Mj - Ms) + 0.023 * sin(2 * Mj - 3 * Ms + 52) - 0.016 * sin(Mj - 5 * Ms - 69))
        elif body == "saturn":
            lon += (0.812 * sin(2 * Mj - 5 * Ms - 67.6) - 0.229 * cos(2 * Mj - 4 * Ms - 2) + 0.119 * sin(Mj - 2 * Ms - 3)
                    + 0.046 * sin(2 * Mj - 6 * Ms - 69) + 0.014 * sin(Mj - 3 * Ms + 32))
            lat += -0.020 * cos(2 * Mj - 4 * Ms - 2) + 0.018 * sin(2 * Mj - 6 * Ms - 49)
        else:
            lon += 0.040 * sin(Ms - 2 * Mu + 6) + 0.035 * sin(Ms - 3 * Mu + 33) - 0.015 * sin(Mj - Mu + 20)
        xh, yh, zh = r * cos(lon) * cos(lat), r * sin(lon) * cos(lat), r * sin(lat)
    return xh, yh, zh


def _pluto_helio(d):
    S, P = 50.03 + 0.033459652 * d, 238.95 + 0.003968789 * d
    lon = (238.9508 + 0.00400703 * d - 19.799 * sin(P) + 19.848 * cos(P) + 0.897 * sin(2 * P) - 4.956 * cos(2 * P)
           + 0.610 * sin(3 * P) + 1.211 * cos(3 * P) - 0.341 * sin(4 * P) - 0.190 * cos(4 * P) + 0.128 * sin(5 * P)
           - 0.034 * cos(5 * P) - 0.038 * sin(6 * P) + 0.031 * cos(6 * P) + 0.020 * sin(S - P) - 0.010 * cos(S - P))
    lat = (-3.9082 - 5.453 * sin(P) - 14.975 * cos(P) + 3.527 * sin(2 * P) + 1.673 * cos(2 * P) - 1.051 * sin(3 * P)
           + 0.328 * cos(3 * P) + 0.179 * sin(4 * P) - 0.292 * cos(4 * P) + 0.019 * sin(5 * P) + 0.100 * cos(5 * P)
           - 0.031 * sin(6 * P) - 0.026 * cos(6 * P) + 0.011 * cos(S - P))
    r = (40.72 + 6.68 * sin(P) + 6.90 * cos(P) - 1.18 * sin(2 * P) - 0.03 * cos(2 * P) + 0.15 * sin(3 * P) - 0.14 * cos(3 * P))
    return r * cos(lon) * cos(lat), r * sin(lon) * cos(lat), r * sin(lat)


def _moon_lon(d):
    xh, yh, zh, r = _orbit("moon", d)
    lon = atan2(yh, xh)
    Nm, _, wm, _, _, Mm = _elements("moon", d)
    _, _, ws, _, _, Ms = _elements("sun", d)
    Ls, Lm = Ms + ws, Mm + wm + Nm
    D, F = Lm - Ls, Lm - Nm
    lon += (-1.274 * sin(Mm - 2 * D) + 0.658 * sin(2 * D) - 0.186 * sin(Ms) - 0.059 * sin(2 * Mm - 2 * D)
            - 0.057 * sin(Mm - 2 * D + Ms) + 0.053 * sin(Mm + 2 * D) + 0.046 * sin(2 * D - Ms) + 0.041 * sin(Mm - Ms)
            - 0.035 * sin(D) - 0.031 * sin(Mm + Ms) - 0.015 * sin(2 * F - 2 * D) + 0.011 * sin(Mm - 4 * D))
    return norm(lon)


def _lon_j2000(body, d):
    if body == "moon":
        return _moon_lon(d)
    slon, sr = _sun(d)
    if body == "sun":
        return slon
    xh, yh, zh = _pluto_helio(d) if body == "pluto" else _planet_helio(body, d)
    xg, yg = xh + sr * cos(slon), yh + sr * sin(slon)
    return norm(atan2(yg, xg))


def ecliptic_longitude(body, d):
    """Геоцентрическая долгота тела (градусы, равноденствие даты) на день d."""
    if body == "node":  # средний восходящий лунный узел (формула Ж. Мееуса)
        T = (d - 1.5) / 36525.0
        return norm(125.04452 - 1934.136261 * T + 0.0020708 * T * T + T ** 3 / 450000.0)
    # элементы орбит даны для J2000, поэтому добавляем общую прецессию с 2000 года (около 50,3″ в год)
    return norm(_lon_j2000(body, d) + 3.82394e-5 * (d - 1.5))


def _diff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def is_retrograde(body, d):
    if body in ("sun", "moon", "node"):
        return False
    return _diff(ecliptic_longitude(body, d + 0.25), ecliptic_longitude(body, d - 0.25)) < 0


def obliquity(jd):
    return 23.4392911 - 0.0130042 * (jd - 2451545.0) / 36525.0


def sidereal_deg(jd):
    T = (jd - 2451545.0) / 36525.0
    return norm(280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T - T ** 3 / 38710000.0)


def _asc_mc(ramc, lat, eps):
    mc = norm(atan2(sin(ramc), cos(ramc) * cos(eps)))
    asc = norm(atan2(cos(ramc), -(sin(ramc) * cos(eps) + tan(lat) * sin(eps))))
    return asc, mc


def _placidus(ramc, lat, eps, start, frac, above):
    ra = ramc + start
    for _ in range(60):
        lam = atan2(sin(ra), cos(ra) * cos(eps))
        dec = math.degrees(math.asin(sin(eps) * sin(lam)))
        x = -tan(lat) * tan(dec)
        if abs(x) >= 1:
            raise ValueError("полярная широта")
        sa = math.degrees(math.acos(x))  # полудуга над горизонтом
        new = ramc + frac * sa if above else ramc + 180 - frac * (180 - sa)
        if abs(new - ra) < 1e-7:
            ra = new
            break
        ra = new
    return norm(atan2(sin(ra), cos(ra) * cos(eps)))


def houses(jd, lat, lon_east):
    """Куспиды 12 домов (индекс 0 — первый дом), ASC, MC и название системы."""
    eps = obliquity(jd)
    ramc = norm(sidereal_deg(jd) + lon_east)
    asc, mc = _asc_mc(ramc, lat, eps)
    system = "Плацидус"
    try:
        if abs(lat) > 66:
            raise ValueError
        c11 = _placidus(ramc, lat, eps, 30, 1 / 3, True)
        c12 = _placidus(ramc, lat, eps, 60, 2 / 3, True)
        c2 = _placidus(ramc, lat, eps, 120, 2 / 3, False)
        c3 = _placidus(ramc, lat, eps, 150, 1 / 3, False)
        cusps = [asc, c2, c3, norm(mc + 180), norm(c11 + 180), norm(c12 + 180), norm(asc + 180), norm(c2 + 180),
                 norm(c3 + 180), mc, c11, c12]
    except ValueError:
        system = "равные дома"
        cusps = [norm(asc + 30 * k) for k in range(12)]
    return cusps, asc, mc, system


def house_of(lon, cusps):
    for k in range(12):
        a, b = cusps[k], cusps[(k + 1) % 12]
        if (lon - a) % 360 < (b - a) % 360:
            return k + 1
    return 1


def sign_info(lon):
    k = int(lon // 30) % 12
    return {"sign": SIGNS[k], "sign_index": k, "deg": round(lon % 30, 2)}


def local_to_utc(birth_date, birth_time, tz_name):
    return datetime.combine(birth_date, birth_time, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def build_chart(birth_date: date, birth_time, lat, lon_east, tz_name):
    """Собирает карту. Если время рождения неизвестно (None), берём полдень и не считаем дома и асцендент."""
    known = birth_time is not None
    utc = local_to_utc(birth_date, birth_time or time(12, 0), tz_name)
    jd = julian_day(utc)
    d = jd - 2451543.5
    cusps = asc = mc = system = None
    if known:
        cusps, asc, mc, system = houses(jd, lat, lon_east)
    planets = []
    for body in PLANETS:
        lon = ecliptic_longitude(body, d)
        p = {"id": body, "name": PLANET_NAMES[body], "lon": round(lon, 3), **sign_info(lon),
             "retro": is_retrograde(body, d) if body != "node" else False}
        p["house"] = house_of(lon, cusps) if cusps else None
        planets.append(p)
    main = [p for p in planets if p["id"] != "node"]
    aspects = []
    for i, a in enumerate(main):
        for b in main[i + 1:]:
            gap = abs(_diff(a["lon"], b["lon"]))
            for key, ru, angle, orb in ASPECTS:
                if abs(gap - angle) <= orb:
                    # Луна без точного времени может «уйти» на несколько градусов, такие аспекты не показываем
                    if not known and "moon" in (a["id"], b["id"]):
                        continue
                    aspects.append({"a": a["id"], "b": b["id"], "type": key, "name": ru, "orb": round(abs(gap - angle), 2)})
    aspects.sort(key=lambda x: x["orb"])
    pts = [p for p in main if known or p["id"] != "moon"] + ([{"sign_index": int(asc // 30) % 12}] if known else [])
    elements = {n: 0 for n in ELEMENT}
    modalities = {n: 0 for n in MODALITY}
    for p in pts:
        elements[ELEMENT[p["sign_index"] % 4]] += 1
        modalities[MODALITY[p["sign_index"] % 3]] += 1
    chart = {"planets": planets, "aspects": aspects, "elements": elements, "modalities": modalities, "time_known": known,
             "utc": utc.strftime("%Y-%m-%d %H:%M"), "system": system}
    if known:
        chart["asc"] = {"lon": round(asc, 3), **sign_info(asc)}
        chart["mc"] = {"lon": round(mc, 3), **sign_info(mc)}
        chart["houses"] = [{"n": k + 1, "lon": round(c, 3), **sign_info(c)} for k, c in enumerate(cusps)]
    return chart
