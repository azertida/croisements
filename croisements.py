#!/usr/bin/env python3
"""
Construit la carte des croisements entre lignes STIB.

Sources (API publique, sans cle) :
  - stopsByLine  : pour chaque ligne et chaque sens, la suite ordonnee des points
  - StopDetails  : pour chaque point d'arret, son nom (fr/nl) et ses coordonnees

Deux lignes "se croisent" si l'une dessert un point d'arret situe a moins de
DISTANCE_METRES d'un point desservi par l'autre. On raisonne par paires de
points : aucun regroupement de points en "lieux" n'est construit, donc pas
d'effet de chaine le long d'un boulevard.

Le nom d'arret ne sert plus d'identite, seulement d'etiquette d'affichage.

Sortie : croisements.json
Licence des donnees : CC BY 4.0 - attribution obligatoire (voir champ "source").
"""

import json
import math
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone

BASE = "https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/static"
URL_LIGNES = f"{BASE}/stopsByLine"
URL_ARRETS = f"{BASE}/StopDetails"

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


def parcours_par_ligne(brut, points):
    """
    ligne -> {sens: [ids de points dans l'ordre]}
    Les ids absents de StopDetails sont signales a part.
    """
    parcours = {}
    manquants = {}
    for entree in brut["results"]:
        ligne = str(entree["lineid"]).strip()
        if not ligne_retenue(ligne):
            continue
        sens = entree.get("direction", "").strip()
        suite = []
        for point in sorted(champ_json(entree["points"]), key=lambda p: p["order"]):
            identifiant = str(point["id"])
            if identifiant not in points:
                manquants.setdefault(identifiant, set()).add(ligne)
                continue
            suite.append(identifiant)
        parcours.setdefault(ligne, {})[sens] = suite
    return parcours, manquants


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
        precedent = None
        for identifiant in suite:
            norm = points[identifiant]["norm"]
            if norm == precedent and etapes:
                etapes[-1]["ids"].add(identifiant)
            elif norm in connus:
                connus[norm]["ids"].add(identifiant)
            else:
                etape = {"nom": points[identifiant]["nom"],
                         "norm": norm,
                         "ids": {identifiant}}
                etapes.append(etape)
                connus[norm] = etape
            precedent = norm
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

def paires_par_nom(parcours, points):
    """Paires de lignes qui partagent un nom d'arret normalise."""
    desserte = {}
    for ligne, sens_disponibles in parcours.items():
        for suite in sens_disponibles.values():
            for identifiant in suite:
                desserte.setdefault(points[identifiant]["norm"], set()).add(ligne)
    paires = {}
    for norm, lignes in desserte.items():
        ordonnees = sorted(lignes, key=cle_tri)
        for i, a in enumerate(ordonnees):
            for b in ordonnees[i + 1:]:
                paires.setdefault((a, b), set()).add(norm)
    return paires


# --------------------------------------------------------------------------

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

    points = indexer_points(brut_points)
    parcours, manquants = parcours_par_ligne(brut_lignes, points)
    if not parcours:
        raise RuntimeError("Aucune ligne reguliere retenue : format des donnees change ?")

    # Quelles lignes passent par chaque point ?
    lignes_par_point = {}
    for ligne, sens_disponibles in parcours.items():
        for suite in sens_disponibles.values():
            for identifiant in suite:
                lignes_par_point.setdefault(identifiant, set()).add(ligne)

    grille, pas_lat, pas_lon = construire_grille(
        points, lignes_par_point.keys(), lignes_par_point)

    etapes_par_ligne = {
        ligne: etapes_ordonnees(sens_disponibles, points)
        for ligne, sens_disponibles in parcours.items()
    }

    # Croisements : pour chaque etape, les lignes joignables a pied.
    for ligne, etapes in etapes_par_ligne.items():
        for etape in etapes:
            proches = set()
            for identifiant in etape["ids"]:
                proches |= lignes_a_proximite(
                    identifiant, points, grille, pas_lat, pas_lon, lignes_par_point)
            etape["croisements"] = sorted(proches - {ligne}, key=cle_tri)

    lignes = {}
    for ligne in sorted(etapes_par_ligne, key=cle_tri):
        lignes[ligne] = {
            "sens_reference": (SENS_REFERENCE if SENS_REFERENCE in parcours[ligne]
                               else next(iter(parcours[ligne]))),
            "arrets": [{"arret": e["nom"], "croisements": e["croisements"]}
                       for e in etapes_par_ligne[ligne] if e["croisements"]],
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

    # Controle : ecarts avec la methode par egalite des noms.
    par_distance = {}
    for ligne, etapes in etapes_par_ligne.items():
        for etape in etapes:
            for autre in etape["croisements"]:
                paire = tuple(sorted((ligne, autre), key=cle_tri))
                par_distance.setdefault(paire, set()).add(etape["nom"])
    par_nom = paires_par_nom(parcours, points)

    gagnes = sorted(set(par_distance) - set(par_nom), key=lambda p: (cle_tri(p[0]), cle_tri(p[1])))
    perdus = sorted(set(par_nom) - set(par_distance), key=lambda p: (cle_tri(p[0]), cle_tri(p[1])))

    return {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "STIB-MIVB - Open Data - "
                  + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "licence": "CC BY 4.0",
        "distance_metres": DISTANCE_METRES,
        "lignes": lignes,
        "doublons": doublons,
        "controle": {
            "lignes_retenues": len(parcours),
            "points_localises": len(points),
            "identifiants_sans_nom": len(manquants),
            "paires_gagnees": len(gagnes),
            "paires_perdues": len(perdus),
            # Paires de lignes que la distance revele et que le nom ignorait :
            # a valider, ce sont les cas type Crainhem / Kraainem.
            "exemples_gagnees": [
                {"lignes": list(paire), "via": sorted(par_distance[paire])[:3]}
                for paire in gagnes[:ECHANTILLON_CONTROLE]
            ],
            # Paires que le nom donnait et que la distance ne voit pas :
            # deux arrets homonymes eloignes. Si la liste est longue,
            # le seuil est trop court.
            "exemples_perdues": [
                {"lignes": list(paire), "arrets": sorted(par_nom[paire])[:3]}
                for paire in perdus[:ECHANTILLON_CONTROLE]
            ],
        },
    }


if __name__ == "__main__":
    resultat = construire()
    with open(SORTIE, "w", encoding="utf-8") as fichier:
        json.dump(resultat, fichier, ensure_ascii=False, separators=(",", ":"))

    c = resultat["controle"]
    print(f"{c['lignes_retenues']} lignes, {c['points_localises']} points localises, "
          f"{len(resultat['doublons'])} doublons de parcours")
    print(f"seuil {DISTANCE_METRES} m : "
          f"{c['paires_gagnees']} paires gagnees, {c['paires_perdues']} perdues")
    if c["identifiants_sans_nom"]:
        print(f"ATTENTION : {c['identifiants_sans_nom']} points absents de StopDetails")
