#!/usr/bin/env python3
"""
Construit la carte des croisements entre lignes STIB.

Sources (API publique, sans cle) :
  - stopsByLine  : pour chaque ligne et chaque sens, la suite ordonnee des points
  - StopDetails  : pour chaque point d'arret, son nom (fr/nl) et ses coordonnees

Deux lignes "se croisent" si elles desservent un arret de meme nom, OU si
deux de leurs points d'arret sont distants de moins de DISTANCE_METRES.
Les deux criteres s'additionnent : l'homonymie rattrape les arrets etales
(De Brouckere), la proximite rattrape les denominations divergentes entre
metro et bus (Crainhem / Kraainem).

La proximite se calcule par paires de points, sans construire de "lieux" :
pas d'effet de chaine le long d'un boulevard.

Les archives GTFS ajoutent trois choses :
  - le mode de transport de chaque ligne STIB (metro, tram, bus)
  - les gares SNCB proches d'un arret
  - quelques lignes TEC et De Lijn choisies a la main

Sortie : croisements.json
Licence des donnees : CC BY 4.0 - attribution obligatoire (voir champ "source").
"""

import csv
import io
import json
import math
import os
import re
import tempfile
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timezone

BASE = "https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/static"
URL_LIGNES = f"{BASE}/stopsByLine"
URL_ARRETS = f"{BASE}/StopDetails"

GTFS = "https://api-management-discovery-production.azure-api.net/api/gtfs/feed"
URL_GTFS_STIB = f"{GTFS}/stibmivb/static"
URL_GTFS_SNCB = f"{GTFS}/nmbssncb/static"

# Lignes des reseaux voisins que l'on veut voir apparaitre, et elles seules.
RESEAUX_VOISINS = [
    ("TEC",     f"{GTFS}/tec/static",     {"W", "365"}),
    ("De Lijn", f"{GTFS}/delijn/static",  {"R36", "R55"}),
]

# Cadre large autour de Bruxelles, pour ecarter le reste du pays.
CADRE = (50.72, 50.96, 4.20, 4.52)   # lat min, lat max, lon min, lon max

MODES = {"0": "tram", "1": "metro", "2": "train", "3": "bus"}

SENS_REFERENCE = "City"          # sens qui fixe l'ordre d'affichage
DISTANCE_METRES = 150            # seuil de proximite entre deux points d'arret
LONGUEUR_DOUBLON = 4             # arrets consecutifs partages = doublon de parcours
ECHANTILLON_CONTROLE = 40        # nb max d'ecarts listes dans le bloc de controle
SORTIE = "croisements.json"

# Bruxelles : conversion degres -> metres (approximation locale suffisante
# a cette echelle, l'erreur est tres inferieure au seuil).
METRES_PAR_DEGRE_LAT = 111320.0
METRES_PAR_DEGRE_LON = 111320.0 * math.cos(math.radians(50.85))


# --------------------------------------------------------------------------
# Recuperation
# --------------------------------------------------------------------------

def telecharger(url, essais=3):
    """Recupere un JSON. Reessaie : le quota anonyme est de 10 requetes/minute."""
    import time
    derniere = None
    for tentative in range(essais):
        try:
            requete = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(requete, timeout=60) as reponse:
                return json.loads(reponse.read().decode("utf-8"))
        except Exception as erreur:          # noqa: BLE001
            derniere = erreur
            if tentative < essais - 1:
                time.sleep(20 * (tentative + 1))
    raise RuntimeError(f"Echec du telechargement de {url} : {derniere}")


def champ_json(valeur):
    """L'API imbrique du JSON dans des chaines. Les deux cas doivent passer."""
    if isinstance(valeur, str):
        return json.loads(valeur)
    return valeur


# --------------------------------------------------------------------------
# Noms (etiquettes d'affichage uniquement)
# --------------------------------------------------------------------------

def normaliser(nom):
    """Forme comparable d'un nom : sert au controle, plus a l'identite."""
    texte = unicodedata.normalize("NFD", nom)
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    texte = texte.upper().replace("'", " ").replace("\u2019", " ")
    texte = texte.replace("-", " ").replace(".", " ")
    texte = re.sub(r"[^A-Z0-9 ]", " ", texte)
    return re.sub(r"\s+", " ", texte).strip()


def ligne_retenue(identifiant):
    """Lignes regulieres seulement : exclut les T (remplacement) et N (nuit)."""
    return identifiant.isdigit()


def cle_tri(ligne):
    return (len(ligne), ligne)


# --------------------------------------------------------------------------
# Lecture des donnees
# --------------------------------------------------------------------------

def indexer_points(brut):
    """id de point -> {nom, norm, lat, lon}."""
    index = {}
    for arret in brut["results"]:
        noms = champ_json(arret["name"])
        affichable = (noms.get("fr") or noms.get("nl") or "").strip()
        coords = champ_json(arret["gpscoordinates"])
        if not affichable or coords is None:
            continue
        try:
            lat = float(coords["latitude"])
            lon = float(coords["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        index[str(arret["id"])] = {
            "nom": affichable,
            "norm": normaliser(affichable),
            "lat": lat,
            "lon": lon,
        }
    return index


def parcours_par_ligne(brut, points, secours=None):
    """
    ligne -> {sens: [ids de points dans l'ordre]}
    Un point absent de StopDetails est cherche dans stops.txt, puis sous sa
    racine. Ce qui reste introuvable est signale a part.
    """
    secours = secours or {}
    parcours = {}
    manquants = {}
    recuperes = set()
    for entree in brut["results"]:
        ligne = str(entree["lineid"]).strip()
        if not ligne_retenue(ligne):
            continue
        sens = entree.get("direction", "").strip()
        suite = []
        for point in sorted(champ_json(entree["points"]), key=lambda p: p["order"]):
            identifiant = str(point["id"])
            if identifiant not in points:
                trouve = secours.get(identifiant) or secours.get(racine(identifiant) or "")
                if trouve:
                    nom, lat, lon = trouve
                    points[identifiant] = {"nom": nom, "norm": normaliser(nom),
                                           "lat": lat, "lon": lon}
                    recuperes.add(identifiant)
                else:
                    manquants.setdefault(identifiant, set()).add(ligne)
                    continue
            suite.append(identifiant)
        parcours.setdefault(ligne, {})[sens] = suite
    return parcours, manquants, recuperes


def etapes_ordonnees(sens_disponibles, points):
    """
    Suite ordonnee d'etapes pour une ligne. Une etape regroupe les points
    consecutifs de meme nom (les deux quais d'un meme arret) et porte la
    liste de leurs ids. Une etape deja vue plus haut n'est pas repetee.
    """
    reference = sens_disponibles.get(SENS_REFERENCE)
    if reference is None:
        reference = next(iter(sens_disponibles.values()))

    etapes, connus = [], {}
    for suite in [reference] + list(sens_disponibles.values()):
        for identifiant in suite:
            norm = points[identifiant]["norm"]
            if norm in connus:
                # Deja rencontre : autre quai, ou passage en sens inverse.
                connus[norm]["ids"].add(identifiant)
            else:
                etape = {"nom": points[identifiant]["nom"],
                         "norm": norm,
                         "ids": {identifiant}}
                etapes.append(etape)
                connus[norm] = etape
    return etapes


# --------------------------------------------------------------------------
# Proximite geographique
# --------------------------------------------------------------------------

def construire_grille(points, ids_utilises, lignes_par_point):
    """
    Decoupe l'espace en cases de DISTANCE_METRES de cote.
    Comparer une case et ses huit voisines suffit a couvrir le seuil.
    """
    grille = {}
    pas_lat = DISTANCE_METRES / METRES_PAR_DEGRE_LAT
    pas_lon = DISTANCE_METRES / METRES_PAR_DEGRE_LON
    for identifiant in ids_utilises:
        p = points[identifiant]
        case = (int(p["lat"] / pas_lat), int(p["lon"] / pas_lon))
        grille.setdefault(case, []).append(identifiant)
    return grille, pas_lat, pas_lon


def distance_metres(a, b):
    dlat = (a["lat"] - b["lat"]) * METRES_PAR_DEGRE_LAT
    dlon = (a["lon"] - b["lon"]) * METRES_PAR_DEGRE_LON
    return math.hypot(dlat, dlon)


def lignes_a_proximite(identifiant, points, grille, pas_lat, pas_lon,
                       lignes_par_point):
    """Lignes desservant un point situe a moins du seuil de celui-ci."""
    p = points[identifiant]
    base_lat = int(p["lat"] / pas_lat)
    base_lon = int(p["lon"] / pas_lon)
    trouvees = set()
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for voisin in grille.get((base_lat + dlat, base_lon + dlon), ()):
                if voisin == identifiant:
                    trouvees |= lignes_par_point[voisin]
                elif distance_metres(p, points[voisin]) <= DISTANCE_METRES:
                    trouvees |= lignes_par_point[voisin]
    return trouvees


# --------------------------------------------------------------------------
# Controle : comparaison avec l'ancienne methode (egalite des noms)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Archives GTFS
# --------------------------------------------------------------------------

def telecharger_archive(url, chemin):
    """Ecrit l'archive sur disque : zipfile a besoin d'un fichier navigable."""
    requete = urllib.request.Request(url, headers={"Accept": "application/zip"})
    with urllib.request.urlopen(requete, timeout=300) as reponse, \
         open(chemin, "wb") as fichier:
        while True:
            morceau = reponse.read(1 << 20)
            if not morceau:
                break
            fichier.write(morceau)
    return os.path.getsize(chemin)


def lire_table(archive, nom):
    """
    Parcourt un fichier du GTFS ligne a ligne, sans le charger en entier.
    stop_times.txt peut peser plusieurs centaines de megaoctets.
    """
    with archive.open(nom) as brut:
        flux = io.TextIOWrapper(brut, encoding="utf-8-sig", newline="")
        for enregistrement in csv.DictReader(flux):
            yield enregistrement


def dans_le_cadre(lat, lon):
    lat_min, lat_max, lon_min, lon_max = CADRE
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def coordonnees(enregistrement):
    try:
        return (float(enregistrement["stop_lat"]),
                float(enregistrement["stop_lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def modes_des_lignes(chemin):
    """route_short_name -> mode, pour les lignes STIB."""
    modes = {}
    with zipfile.ZipFile(chemin) as archive:
        for route in lire_table(archive, "routes.txt"):
            nom = (route.get("route_short_name") or "").strip()
            if nom:
                modes[nom] = MODES.get((route.get("route_type") or "").strip(),
                                       "bus")
    return modes


def arrets_du_gtfs(chemin):
    """
    Tous les arrets de stops.txt : id -> (nom, lat, lon).
    Sert de secours quand StopDetails ignore un point d'arret.
    """
    secours = {}
    with zipfile.ZipFile(chemin) as archive:
        for arret in lire_table(archive, "stops.txt"):
            position = coordonnees(arret)
            nom = (arret.get("stop_name") or "").strip().strip('"')
            if position and nom:
                secours[str(arret.get("stop_id")).strip()] = (nom, *position)
    return secours


def racine(identifiant):
    """
    2217F -> 2217. Les points d'arret suffixes absents de stops.txt y sont
    souvent presents sous leur racine, qui designe le meme lieu de l'autre
    cote de la chaussee. L'ecart est de quelques dizaines de metres, tres
    en deca du seuil de proximite.
    """
    if identifiant and not identifiant[-1].isdigit():
        return identifiant[:-1]
    return None


def gares_du_cadre(chemin):
    """
    Gares SNCB autour de Bruxelles. On ne garde que les gares meres :
    sans elles, chaque quai de Bruxelles-Midi compterait pour une gare.
    """
    gares = []
    with zipfile.ZipFile(chemin) as archive:
        for arret in lire_table(archive, "stops.txt"):
            if (arret.get("parent_station") or "").strip():
                continue                      # c'est un quai, pas une gare
            position = coordonnees(arret)
            if not position or not dans_le_cadre(*position):
                continue
            nom = (arret.get("stop_name") or "").strip()
            if nom:
                gares.append((nom, position[0], position[1]))
    return gares


def arrets_de_lignes(chemin, lignes_voulues):
    """
    Arrets desservis par quelques lignes precises d'un reseau voisin.
    Trois passes : routes -> trips -> stop_times, puis les coordonnees.
    Renvoie (liste d'arrets, noms de lignes reellement trouves).
    """
    with zipfile.ZipFile(chemin) as archive:

        routes, trouvees = {}, set()
        for route in lire_table(archive, "routes.txt"):
            nom = (route.get("route_short_name") or "").strip()
            if nom in lignes_voulues:
                routes[route["route_id"]] = nom
                trouvees.add(nom)
        if not routes:
            return [], trouvees

        trajets = {}
        for trajet in lire_table(archive, "trips.txt"):
            ligne = routes.get(trajet.get("route_id"))
            if ligne:
                trajets[trajet["trip_id"]] = ligne
        if not trajets:
            return [], trouvees

        # Le gros fichier : parcouru une seule fois, en flux.
        lignes_par_arret = {}
        for passage in lire_table(archive, "stop_times.txt"):
            ligne = trajets.get(passage.get("trip_id"))
            if ligne:
                lignes_par_arret.setdefault(passage["stop_id"], set()).add(ligne)
        if not lignes_par_arret:
            return [], trouvees

        arrets = []
        for arret in lire_table(archive, "stops.txt"):
            desservi = lignes_par_arret.get(arret.get("stop_id"))
            if not desservi:
                continue
            position = coordonnees(arret)
            if not position or not dans_le_cadre(*position):
                continue
            for ligne in sorted(desservi):
                arrets.append((ligne, position[0], position[1]))
    return arrets, trouvees


def voisinage(points_externes):
    """
    Petite grille pour retrouver rapidement ce qui se trouve pres d'un point.
    points_externes : liste de (etiquette, lat, lon).
    """
    grille = {}
    pas_lat = DISTANCE_METRES / METRES_PAR_DEGRE_LAT
    pas_lon = DISTANCE_METRES / METRES_PAR_DEGRE_LON
    for etiquette, lat, lon in points_externes:
        case = (int(lat / pas_lat), int(lon / pas_lon))
        grille.setdefault(case, []).append((etiquette, lat, lon))
    return grille, pas_lat, pas_lon


def etiquettes_proches(point, grille, pas_lat, pas_lon):
    base_lat = int(point["lat"] / pas_lat)
    base_lon = int(point["lon"] / pas_lon)
    trouvees = set()
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for etiquette, lat, lon in grille.get(
                    (base_lat + dlat, base_lon + dlon), ()):
                if distance_metres(point, {"lat": lat, "lon": lon}) <= DISTANCE_METRES:
                    trouvees.add(etiquette)
    return trouvees


# --------------------------------------------------------------------------

def arret_en_sortie(etape):
    """Un arret : son nom, ses correspondances STIB, puis les apports voisins."""
    sortie = {"arret": etape["nom"], "croisements": etape["croisements"]}
    if etape.get("gares"):
        sortie["gares"] = etape["gares"]
    if etape.get("voisins"):
        sortie["voisins"] = [{"reseau": r, "ligne": l} for r, l in etape["voisins"]]
    return sortie


def construire():
    brut_lignes = telecharger(URL_LIGNES)
    brut_points = telecharger(URL_ARRETS)

    annonce = brut_lignes.get("totalCount")
    recu = len(brut_lignes.get("results", []))
    if annonce is not None and recu < annonce:
        raise RuntimeError(
            f"Reponse incomplete : {recu} entrees recues sur {annonce} annoncees. "
            "L'API pagine peut-etre desormais."
        )

    dossier = tempfile.mkdtemp()
    chemin_stib = os.path.join(dossier, "stib.zip")
    telecharger_archive(URL_GTFS_STIB, chemin_stib)
    modes = modes_des_lignes(chemin_stib)
    secours = arrets_du_gtfs(chemin_stib)
    os.remove(chemin_stib)

    points = indexer_points(brut_points)
    parcours, manquants, recuperes = parcours_par_ligne(
        brut_lignes, points, secours)
    if not parcours:
        raise RuntimeError("Aucune ligne reguliere retenue : format des donnees change ?")

    # Quelles lignes passent par chaque point ?
    lignes_par_point = {}
    for ligne, sens_disponibles in parcours.items():
        for suite in sens_disponibles.values():
            for identifiant in suite:
                lignes_par_point.setdefault(identifiant, set()).add(ligne)

    # Quelles lignes desservent un arret portant ce nom ?
    lignes_par_nom = {}
    for identifiant, lignes_desservantes in lignes_par_point.items():
        lignes_par_nom.setdefault(points[identifiant]["norm"], set()).update(
            lignes_desservantes)

    grille, pas_lat, pas_lon = construire_grille(
        points, lignes_par_point.keys(), lignes_par_point)

    etapes_par_ligne = {
        ligne: etapes_ordonnees(sens_disponibles, points)
        for ligne, sens_disponibles in parcours.items()
    }

    # Croisements : meme nom d'arret OU points a moins du seuil.
    # Les deux criteres sont additionnes, jamais opposes : chacun rattrape
    # les cas ou l'autre echoue.
    origines = {}
    for ligne, etapes in etapes_par_ligne.items():
        for etape in etapes:
            par_proximite = set()
            for identifiant in etape["ids"]:
                par_proximite |= lignes_a_proximite(
                    identifiant, points, grille, pas_lat, pas_lon, lignes_par_point)
            par_homonymie = lignes_par_nom.get(etape["norm"], set())
            etape["croisements"] = sorted(
                (par_proximite | par_homonymie) - {ligne}, key=cle_tri)
            for autre in etape["croisements"]:
                paire = tuple(sorted((ligne, autre), key=cle_tri))
                trace = origines.setdefault(paire, {"nom": False, "distance": False,
                                                    "via": set()})
                trace["via"].add(etape["nom"])
                if autre in par_homonymie:
                    trace["nom"] = True
                if autre in par_proximite:
                    trace["distance"] = True

    # ---- apports des archives GTFS ----
    gares, voisins, trouvees, absentes = [], [], set(), set()
    try:
        chemin = os.path.join(dossier, "sncb.zip")
        telecharger_archive(URL_GTFS_SNCB, chemin)
        gares = [(nom, lat, lon) for nom, lat, lon in gares_du_cadre(chemin)]
        os.remove(chemin)

        for reseau, url, voulues in RESEAUX_VOISINS:
            chemin = os.path.join(dossier, reseau.replace(" ", "") + ".zip")
            telecharger_archive(url, chemin)
            arrets, presentes = arrets_de_lignes(chemin, voulues)
            for ligne, lat, lon in arrets:
                voisins.append(((reseau, ligne), lat, lon))
            trouvees |= {(reseau, l) for l in presentes}
            absentes |= {(reseau, l) for l in voulues - presentes}
            os.remove(chemin)
    finally:
        for reste in os.listdir(dossier):
            os.remove(os.path.join(dossier, reste))
        os.rmdir(dossier)

    grille_gares, gl, gn = voisinage([(nom, lat, lon) for nom, lat, lon in gares])
    grille_voisins, vl, vn = voisinage(voisins)

    for etapes in etapes_par_ligne.values():
        for etape in etapes:
            proches_gares, proches_voisins = set(), set()
            for identifiant in etape["ids"]:
                point = points[identifiant]
                proches_gares |= etiquettes_proches(point, grille_gares, gl, gn)
                proches_voisins |= etiquettes_proches(point, grille_voisins, vl, vn)
            etape["gares"] = sorted(proches_gares)
            etape["voisins"] = sorted(proches_voisins,
                                      key=lambda p: (p[0], cle_tri(p[1])))

    lignes = {}
    for ligne in sorted(etapes_par_ligne, key=cle_tri):
        lignes[ligne] = {
            "sens_reference": (SENS_REFERENCE if SENS_REFERENCE in parcours[ligne]
                               else next(iter(parcours[ligne]))),
            "mode": modes.get(ligne, "bus"),
            # Tous les arrets, dans l'ordre : un fil troue ne se lit plus
            # comme un parcours. Ceux sans correspondance ont une liste vide.
            "arrets": [arret_en_sortie(e) for e in etapes_par_ligne[ligne]],
        }

    # Doublons de parcours : suites d'etapes consecutives partagees.
    doublons = []
    noms_lignes = sorted(etapes_par_ligne, key=cle_tri)
    for i, a in enumerate(noms_lignes):
        for b in noms_lignes[i + 1:]:
            meilleure, courante = [], []
            for etape in etapes_par_ligne[a]:
                if b in etape["croisements"]:
                    courante.append(etape["nom"])
                    if len(courante) > len(meilleure):
                        meilleure = list(courante)
                else:
                    courante = []
            if len(meilleure) >= LONGUEUR_DOUBLON:
                doublons.append({"lignes": [a, b], "arrets": meilleure})
    doublons.sort(key=lambda d: len(d["arrets"]), reverse=True)

    # Controle : d'ou vient chaque paire de lignes ?
    seulement_distance = sorted(
        (p for p, t in origines.items() if t["distance"] and not t["nom"]),
        key=lambda p: (cle_tri(p[0]), cle_tri(p[1])))
    seulement_nom = sorted(
        (p for p, t in origines.items() if t["nom"] and not t["distance"]),
        key=lambda p: (cle_tri(p[0]), cle_tri(p[1])))

    return {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "STIB-MIVB, NMBS-SNCB, TEC, De Lijn - Open Data - "
                  + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "licence": "CC BY 4.0",
        "distance_metres": DISTANCE_METRES,
        "lignes": lignes,
        "doublons": doublons,
        "controle": {
            "lignes_retenues": len(parcours),
            "points_localises": len(points),
            "points_recuperes_du_gtfs": len(recuperes),
            "identifiants_sans_nom": len(manquants),
            "paires_total": len(origines),
            # Paires que seule la proximite revele : noms differents de part
            # et d'autre (type Crainhem / Kraainem).
            "paires_par_distance_seule": len(seulement_distance),
            # Paires que seule l'homonymie revele : arrets etales, quais
            # eloignes de plus du seuil (type De Brouckere).
            "paires_par_nom_seul": len(seulement_nom),
            "gares_retenues": len(gares),
            "arrets_voisins": len(voisins),
            # Lignes voisines demandees : celles qui repondent, et les autres.
            "lignes_voisines_trouvees": sorted(f"{r} {l}" for r, l in trouvees),
            "lignes_voisines_absentes": sorted(f"{r} {l}" for r, l in absentes),
            "exemples_distance_seule": [
                {"lignes": list(paire), "via": sorted(origines[paire]["via"])[:3]}
                for paire in seulement_distance[:ECHANTILLON_CONTROLE]
            ],
        },
    }


if __name__ == "__main__":
    resultat = construire()
    with open(SORTIE, "w", encoding="utf-8") as fichier:
        json.dump(resultat, fichier, ensure_ascii=False, separators=(",", ":"))

    c = resultat["controle"]
    print(f"{c['lignes_retenues']} lignes, {c['points_localises']} points localises "
          f"(dont {c['points_recuperes_du_gtfs']} repris du GTFS), "
          f"{len(resultat['doublons'])} doublons de parcours")
    print(f"{c['paires_total']} paires de lignes : "
          f"{c['paires_par_distance_seule']} par la distance seule, "
          f"{c['paires_par_nom_seul']} par le nom seul")
    print(f"{c['gares_retenues']} gares SNCB, "
          f"{c['arrets_voisins']} arrets de reseaux voisins")
    print("lignes voisines trouvees :",
          ", ".join(c["lignes_voisines_trouvees"]) or "aucune")
    if c["lignes_voisines_absentes"]:
        print("ATTENTION, introuvables :",
              ", ".join(c["lignes_voisines_absentes"]))
    if c["identifiants_sans_nom"]:
        print(f"ATTENTION : {c['identifiants_sans_nom']} points absents de StopDetails")
