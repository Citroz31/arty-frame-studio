# Mode VNA : tous les états d'un composant, un balayage par état

Le mode **VNA** de l'onglet [Mesure](mode-mesure.md) caractérise un composant
commandé par une trame (DATA, CLK, LATCH) avec un **analyseur de réseau Keysight
PNA / PNA-X (N5245B…) ou VNA USB Streamline (P9374A…)**. Pour chaque **état** de
la liste, l'application :

1. fixe le niveau de la broche **TR** (3,3 V ou 0 V) si l'état le demande ;
2. envoie le mot, et **vérifie que la carte a terminé la trame** ;
3. laisse passer l'attente de stabilisation (20 ms par défaut) ;
4. déclenche **un balayage unique** du canal choisi et attend sa fin (`*OPC?`) ;
5. relit les **paramètres S de tous les ports** (S11, S21… SNN) ;
6. écrit un fichier **Touchstone** (`.s1p`, `.s2p`, `.s4p`…) et une ligne dans
   `resultats.csv`, puis passe à l'état suivant.

Rien d'autre à décrire : ni champs du mot, ni bits à balayer. On donne **un canal,
un nombre de ports et la liste des mots** ; la liste se prépare dans un tableur ou
un script.

## 1. La liste d'états

Un fichier texte ou CSV (UTF-8), **un état par ligne**, colonnes séparées par
`;`, `,` ou une tabulation. Seul le mot est obligatoire.

```text
# mot ; TR ; nom
mot;TR;nom
000000000000;TX;référence
000000000001;RX;atténuation 0,5 dB
0x0A
```

| Colonne | Contenu |
| --- | --- |
| **mot** | Le mot de la trame complète, en binaire par défaut (zéros de tête compris : leur nombre fixe la largeur), `0x` pour l'hexadécimal, `0b` pour forcer le binaire. 26 bits au plus. |
| **TR** | `TX` ou `RX`, ou directement le niveau de la broche : `1` = 3,3 V, `0` = 0 V. Vide : la broche TR garde son niveau. |
| **nom** | Libre ; repris dans les résultats et dans le commentaire de chaque fichier `.sNp`. |

* **4096 états au plus** (12 bits distincts) ; les mêmes mots peuvent se répéter,
  par exemple une fois en TX et une fois en RX.
* Les lignes vides, ce qui suit `#` et une première ligne d'en-tête sont ignorés.
* Dans l'application : **Mots à envoyer → Liste d'états**, **Charger un fichier
  d'états…** (ou saisie directe) ; l'aperçu indique le nombre d'états, le premier,
  le dernier et le nombre de TX / RX. En ligne de commande : `--states fichier`.
* « Niveau de la broche TR » choisit ce que signifient `TX` et `RX` : par défaut
  **TX = 3,3 V**, RX = 0 V ; l'inverse est un clic.
* Un exemple se trouve dans [`examples/etats_exemple.csv`](../examples/etats_exemple.csv).

## 2. La broche TR

Le firmware de **révision 5** ajoute une sortie statique **TR** (par défaut
**JB4, broche C15**) : elle vaut **3,3 V ou 0 V** selon la dernière commande TR
et ne porte aucune horloge. Elle est à 0 V à la mise sous tension et après un
reset. L'application la positionne **avant** chaque mot qui le demande ; la
broche garde son niveau jusqu'à l'état suivant qui en fixe un autre. Détails et
brochage : [carte et raccordement](hardware.md). Un état qui fixe TR avec un
firmware plus ancien est refusé **avant** tout envoi, avec le message
correspondant.

## 3. Préparer le VNA

À faire une fois sur l'appareil, comme pour une mesure à la main :

* **calibration** de la plage utile, sur les ports utilisés ;
* **plage de fréquence, nombre de points, bande de FI**, éventuellement le
  **moyennage** (l'application le respecte : un groupe de balayages par état) ;
* un **canal** avec cette configuration : on indique son numéro ;
* le **nombre de ports** mesurés (1 à 4) : S11 pour 1 port, S11 S21 S12 S22 pour 2 ports…

L'application crée sur le canal les mesures S manquantes (`AFS_S21`…), **réutilise
celles qui existent déjà**, passe le déclenchement en « immédiat », et à la fin
**supprime ce qu'elle a créé et remet** le mode de balayage (continu ou maintenu)
et le déclenchement d'origine. Rien n'est enregistré dans l'appareil.

Pour lire correctement les fichiers, **ne pas changer le canal ni le nombre de
points pendant la campagne** : un changement est détecté (nombre de valeurs reçues
différent) et arrête la mesure avec un message.

## 4. Connecter le VNA, détection automatique

Dans l'onglet Mesure, **Après chaque mot → VNA Keysight**, puis **Détecter le
VNA**. La recherche essaie, dans l'ordre :

1. **VISA** (Keysight IO Libraries ou NI-VISA) : les VNA **USB** comme le P9374A
   (`USB0::0x2A8D::…::INSTR`) et les appareils **LAN** annoncés par VXI-11
   (`TCPIP0::192.168.x.y::inst0::INSTR`) ;
2. **ce PC, port 5025** : l'application d'un VNA USB sert aussi le SCPI en local
   (127.0.0.1) quand elle tourne ;
3. l'**adresse déjà saisie**, si la liaison est « LAN ».

Chaque candidat reçoit `*IDN?` (rien d'autre n'est envoyé) ; ceux dont le modèle
est un VNA Keysight (N5xxx, P5xxx, P9xxx, M9xxx, E83xx…) passent en premier et le
premier est sélectionné, puis il suffit de **Connecter**. Si rien n'est trouvé :

* **VNA USB** : lancer l'application du VNA sur le PC, installer Keysight IO
  Libraries Suite, puis relancer la détection ;
* **PNA-X en LAN** : saisir son adresse IP (lue sur l'appareil, dans les réglages
  réseau de Windows du PNA) avec la liaison « Keysight · réseau LAN » (port 5025) ;
* **Chercher sur le réseau** essaie le port 5025 des 254 adresses du réseau privé
  de ce PC : à utiliser sur un réseau de laboratoire, pas sur un réseau d'entreprise
  qui surveille les balayages de ports.

En ligne de commande : `arty-frame vna-list [--scan] [--host ADRESSE]`, et
`--vna-auto` dans `sweep` pour se connecter au premier VNA détecté.

## 5. Résultats

Chaque campagne écrit dans un **dossier** (par défaut
`exports/vna-AAAAMMJJ-HHMMSS/`, ou celui qu'on indique) :

```text
exports/vna-20261006-142530/
├── 0001_000000000000_tr1.s2p     rang, mot, niveau de TR
├── 0002_000000000001_tr0.s2p
├── …
└── resultats.csv                 une ligne par état
```

* Les fichiers sont au format **Touchstone 1.0**, `# HZ S RI R 50` (hertz, partie
  réelle et imaginaire, 50 Ω), lisibles par ADS, AWR, QUCS, scikit-rf… Ils sont
  écrits **d'un bloc** : un fichier présent est un fichier complet. L'en-tête dit
  l'état, le mot, TR, le nom, l'instrument, le canal et la date.
* `resultats.csv` : `pas, mot_bin, mot_hex, mot_dec, tr, nom, statut`, la **valeur
  suivie**, `fichier`, `note`, `horodatage`. Il est écrit à la fin de la campagne,
  même interrompue.
* La **valeur suivie** est le module (dB) et la phase d'un paramètre S (S21 par
  défaut) à une fréquence (le milieu de la bande par défaut ; le point de mesure
  le plus proche est utilisé). Elle alimente le tableau, les statistiques, le
  graphique et le critère « Valeur 1 ≥ / ≤ » : un état dont S21 sort de la plage
  est en échec, sans interrompre la campagne sauf si « S'arrêter au premier
  échec » est coché.
* Une erreur signalée par le VNA pendant un balayage (file `SYST:ERR?`) met l'état
  en échec avec le message, le fichier est quand même écrit.
* **Reprise** : après un arrêt, **Reprendre** continue à l'état suivant. Après un
  plantage ou une nouvelle session, indiquer le **dossier de la campagne** et
  cocher **Ne pas remesurer les états déjà enregistrés** : un état dont le fichier
  existe, avec le même nombre de ports et la même plage de fréquence, est relu
  au lieu d'être remesuré.

## 6. Durée

Un état coûte l'envoi du mot (quelques millisecondes en UART), l'attente de
stabilisation, **un balayage du VNA** et le transfert des données (en ASCII : un
paramètre S de 1601 points tient dans une réponse de 40 Ko). La durée totale est
donc celle du balayage multipliée par le nombre d'états : 4096 états de
0,5 s font une trentaine de minutes, avec moyennage ou 16 001 points davantage.
Une estimation du temps restant s'affiche. « Délai max. par balayage » doit
dépasser la durée d'un balayage complet, moyennage compris.

## 7. En ligne de commande

```bash
# Campagne complète, VNA détecté automatiquement, S21 à 5 GHz comme critère
arty-frame sweep --port COM7 --profile trame.json --states etats.csv \
    --probe vna --vna-auto --vna-channel 1 --vna-ports 2 \
    --vna-param S21 --vna-freq 5G --min -30 --vna-dir exports/campagne1

# Reprise du même dossier, sans remesurer ce qui est fait
arty-frame sweep --port COM7 --profile trame.json --states etats.csv \
    --probe vna --vna-lan 192.168.1.60 --vna-dir exports/campagne1 --vna-skip-existing

# Essai sans matériel : carte et VNA simulés
arty-frame sweep --demo --profile examples/frame_26bits.json --states examples/etats_exemple.csv \
    --probe vna --vna-demo --width 12 --vna-dir exports/essai
```

## 8. Commandes SCPI utilisées

Elles suivent le jeu de commandes des PNA ; elles ont été vérifiées contre un
**VNA simulé**, pas encore contre un appareil réel. À la première campagne,
comparer avec le guide de programmation du PNA-X ou du P9374A, et lancer d'abord
2 ou 3 états :

| Étape | Commande |
| --- | --- |
| Canaux présents | `SYST:CHAN:CAT?` |
| Format des données | `FORM:DATA ASCII,0` |
| État d'origine | `SENS<c>:SWE:MODE?`, `TRIG:SOUR?` |
| Mesures du canal | `CALC<c>:PAR:CAT?` |
| Création d'une mesure | `CALC<c>:PAR:DEF:EXT 'AFS_S21','S21'` |
| Moyennage | `SENS<c>:AVER:STAT?`, `SENS<c>:AVER:COUN?` |
| Axe de fréquence | `CALC<c>:PAR:SEL 'nom'`, `CALC<c>:X?` |
| Déclenchement immédiat | `TRIG:SOUR IMM` |
| Balayage unique | `SENS<c>:SWE:MODE SING;*OPC?` |
| Balayage moyenné | `SENS<c>:AVER:CLE`, `SENS<c>:SWE:GRO:COUN n`, `SENS<c>:SWE:MODE GRO;*OPC?` |
| Lecture d'un paramètre S | `CALC<c>:PAR:SEL 'nom'`, `CALC<c>:DATA? SDATA` |
| Fin | `CALC<c>:PAR:DEL 'nom'`, `SENS<c>:SWE:MODE CONT` ou `HOLD`, `TRIG:SOUR <origine>` |
| Erreurs | `SYST:ERR?` |

## 9. Dépannage

| Observation | Action |
| --- | --- |
| « VNA non connecté » | Détecter le VNA, puis Connecter. |
| « Le canal 7 n'existe pas … canaux présents : 1, 2 » | Créer le canal sur le VNA ou choisir un numéro existant. |
| « Création des mesures S … refusé par l'instrument » | Le VNA a moins de ports que demandé, ou le canal ne le permet pas : réduire le nombre de ports. |
| « Délai dépassé » pendant un balayage | Augmenter « Délai max. par balayage » (balayage long, moyennage) et reconnecter. |
| « valeur(s) reçue(s) pour N point(s) » | Le nombre de points a changé sur le VNA pendant la campagne : recommencer. |
| « La simulation connectée est celle du mode SCPI » | Déconnecter, choisir « VNA Keysight », puis reconnecter. |
| « Des états fixent la broche TR … » | Charger le firmware de révision 5 (onglet FPGA) ou retirer la colonne TR. |
| « Trame non confirmée » | La carte n'a pas compté la trame envoyée : reconnecter, vérifier le firmware et la liaison UART. |
| Aucun VNA détecté | Voir la section 4 ; en dernier recours, saisir l'adresse à la main. |
