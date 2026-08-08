#!/usr/bin/env python3
"""
Construit la carte des croisements entre lignes STIB.

Sources (API publique, sans clé) :
  - stopsByLine  : pour chaque ligne et chaque sens, la suite ordonnee des arrets
  - StopDetails  : pour chaque arret, son nom (fr/nl) et ses coordonnees

Deux lignes "se croisent" si elles desservent deux arrets portant le meme nom
normalise. Deux lignes "se doublent" si elles partagent une suite d'au moins
LONGUEUR_DOUBLON arrets consecutifs.

Sortie : croisements.json
Licence des donnees : CC BY 4.0 - attribution obligatoire (voir champ "source").
"""

import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone

BASE = "https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/static"
URL_LIGNES = f"{BASE}/stopsByLine"
URL_ARRETS = f"{BASE}/StopDetails"

SENS_REFERENCE = "City"          # sens qui fixe l'ordre d'affichage
LONGUEUR_DOUBLON = 4             # arrets consecutifs partages = doublon de parcours
SORTIE = "croisements.json"


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
# Normalisation des noms d'arret
# --------------------------------------------------------------------------

def normaliser(nom):
    """
    Ramene un nom d'arret a une forme comparable :
    majuscules, sans accents, ponctuation unifiee, espaces tasses.
    C'est le seul endroit ou une erreur ferait disparaitre des croisements
    en silence.
    """
    texte = unicodedata.normalize("NFD", nom)
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    texte = texte.upper()
    texte = texte.replace("'", " ").replace("'", " ")
    texte = texte.replace("-", " ").replace(".", " ")
    texte = re.sub(r"[^A-Z0-9 ]", " ", texte)
    texte = re.sub(r"\s+", " ", texte).strip()
    return texte


def ligne_retenue(identifiant):
    """
    On ne garde que les lignes regulieres.
    Exclut les T (services de remplacement) et les N (noctambus),
    en ne conservant que les identifiants purement numeriques.
    """
    return identifiant.isdigit()


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def indexer_arrets(brut):
    """id d'arret -> (nom normalise, nom affichable)."""
    index = {}
    for arret in brut["results"]:
        noms = champ_json(arret["name"])
        affichable = (noms.get("fr") or noms.get("nl") or "").strip()
        if not affichable:
            continue
        index[str(arret["id"])] = (normaliser(affichable), affichable)
    return index


def parcours_par_ligne(brut, index_arrets):
    """
    ligne -> {sens: [noms normalises dans l'ordre]}
    Les arrets absents de StopDetails sont comptes a part : c'est le
    signal d'alerte si la correspondance se degrade un jour.
    """
    parcours = {}
    manquants = set()
    for entree in brut["results"]:
        ligne = str(entree["lineid"]).strip()
        if not ligne_retenue(ligne):
            continue
        sens = entree.get("direction", "").strip()
        points = sorted(champ_json(entree["points"]), key=lambda p: p["order"])
        suite = []
        for point in points:
            identifiant = str(point["id"])
            if identifiant not in index_arrets:
                manquants.add(identifiant)
                continue
            nom = index_arrets[identifiant][0]
            if not suite or suite[-1] != nom:      # evite les doublons colles
                suite.append(nom)
        parcours.setdefault(ligne, {})[sens] = suite
    return parcours, manquants


def ordre_affichage(sens_disponibles):
    """
    Ordre de lecture : le sens de reference d'abord, puis les arrets
    que seul l'autre sens dessert, ajoutes a la suite.
    """
    reference = sens_disponibles.get(SENS_REFERENCE)
    if reference is None:
        reference = next(iter(sens_disponibles.values()))
    ordonne = list(reference)
    connus = set(ordonne)
    for suite in sens_disponibles.values():
        for nom in suite:
            if nom not in connus:
                ordonne.append(nom)
                connus.add(nom)
    return ordonne


def calculer_croisements(parcours):
    """nom d'arret -> ensemble des lignes qui le desservent."""
    desserte = {}
    for ligne, sens_disponibles in parcours.items():
        for suite in sens_disponibles.values():
            for nom in suite:
                desserte.setdefault(nom, set()).add(ligne)
    return desserte


def detecter_doublons(parcours, ordres):
    """
    Deux lignes se doublent si elles partagent une suite d'arrets consecutifs.
    C'est la meme donnee que les croisements, lue autrement.
    """
    doublons = []
    lignes = sorted(parcours, key=cle_tri)
    for i, ligne_a in enumerate(lignes):
        ensemble_a = set(ordres[ligne_a])
        for ligne_b in lignes[i + 1:]:
            ensemble_b = set(ordres[ligne_b])
            communs = ensemble_a & ensemble_b
            if len(communs) < LONGUEUR_DOUBLON:
                continue
            meilleure, courante = [], []
            for nom in ordres[ligne_a]:
                if nom in communs:
                    courante.append(nom)
                    if len(courante) > len(meilleure):
                        meilleure = list(courante)
                else:
                    courante = []
            if len(meilleure) >= LONGUEUR_DOUBLON:
                doublons.append({
                    "lignes": [ligne_a, ligne_b],
                    "arrets": meilleure,
                })
    doublons.sort(key=lambda d: len(d["arrets"]), reverse=True)
    return doublons


def cle_tri(ligne):
    return (len(ligne), ligne)


# --------------------------------------------------------------------------

def construire():
    brut_lignes = telecharger(URL_LIGNES)
    brut_arrets = telecharger(URL_ARRETS)

    annonce = brut_lignes.get("totalCount")
    recu = len(brut_lignes.get("results", []))
    if annonce is not None and recu < annonce:
        raise RuntimeError(
            f"Reponse incomplete : {recu} entrees recues sur {annonce} annoncees. "
            "L'API pagine peut-etre desormais."
        )

    index_arrets = indexer_arrets(brut_arrets)
    parcours, manquants = parcours_par_ligne(brut_lignes, index_arrets)
    if not parcours:
        raise RuntimeError("Aucune ligne reguliere retenue : format des donnees change ?")

    affichables = {}
    for identifiant, (normalise, affichable) in index_arrets.items():
        affichables.setdefault(normalise, affichable)

    ordres = {ligne: ordre_affichage(sens) for ligne, sens in parcours.items()}
    desserte = calculer_croisements(parcours)

    lignes = {}
    for ligne in sorted(parcours, key=cle_tri):
        arrets = []
        for nom in ordres[ligne]:
            autres = sorted(desserte[nom] - {ligne}, key=cle_tri)
            if not autres:
                continue                      # on ne garde que les points de rebond
            arrets.append({
                "arret": affichables.get(nom, nom),
                "croisements": autres,
            })
        lignes[ligne] = {
            "sens_reference": SENS_REFERENCE if SENS_REFERENCE in parcours[ligne]
                              else next(iter(parcours[ligne])),
            "arrets": arrets,
        }

    return {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "STIB-MIVB - Open Data - "
                  + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "licence": "CC BY 4.0",
        "lignes": lignes,
        "doublons": detecter_doublons(parcours, ordres),
        "controle": {
            "lignes_retenues": len(parcours),
            "arrets_nommes": len(index_arrets),
            "identifiants_sans_nom": len(manquants),
        },
    }


if __name__ == "__main__":
    resultat = construire()
    with open(SORTIE, "w", encoding="utf-8") as fichier:
        json.dump(resultat, fichier, ensure_ascii=False, separators=(",", ":"))

    controle = resultat["controle"]
    print(f"{controle['lignes_retenues']} lignes, "
          f"{controle['arrets_nommes']} arrets nommes, "
          f"{len(resultat['doublons'])} doublons de parcours")
    if controle["identifiants_sans_nom"]:
        print(f"ATTENTION : {controle['identifiants_sans_nom']} "
              f"identifiants d'arret sans nom (ignores)")
