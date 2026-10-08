# Récapitulatif des apports de Claude

Ce document résume tout ce que Claude a apporté au projet du 3 au 8 octobre
2026, avec le lien vers chaque commit sur GitHub. Les commits de Codex et les
fusions faites par le propriétaire du dépôt ne sont cités que pour le contexte.

> **Aucun de ces changements n'a été testé sur une carte Arty réelle.** Les
> preuves sont les tests Python, les bancs de simulation RTL (Icarus Verilog)
> et les compilations réelles sur GitHub Actions. Le manifeste du firmware
> garde `hardware_validated: false`.

## Vue d'ensemble

| Domaine | Apport principal |
| --- | --- |
| Correction critique | Broches UART RX/TX inversées dans le XDC : sans ce correctif, la carte ne pouvait pas répondre. |
| Connectivité | Test des LED LD4–LD7 (commande LED) et identification du firmware (commande INFO). |
| Firmware à la demande | Choix de l'horloge du cœur (6191 valeurs, 6,25–300 MHz) et des broches DATA/CLK/LATCH, compilation locale ou sur GitHub Actions depuis l'interface. |
| Haute fréquence | Moteur de trame révision 6 jusqu'à 300 MHz, planificateur de fréquence (la plus proche ou sans dépasser), vérification du PLL dans le FASM. |
| Émission continue | Répétition de la trame jusqu'à Arrêter (firmware révision 3). |
| CLK libre | Horloge sans interruption pendant LATCH et la pause (firmware révision 4). |
| Timing 200 MHz | Chemins critiques réenregistrés, balayage des graines de placement avec marge de 3 %. |
| Saisie de trame | Binaire par défaut, nombre de bits calculé automatiquement, 26 bits maximum. |
| Journal Windows | Plus d'erreur « toolchain.json » ni de liaison UART fermée pour rien ; avertissements OneDrive et chemins longs. |
| Oscilloscope | Onglet de mesure avec un Keysight InfiniiVision (DSOX1202A) en LAN ou USB, ou simulé : fréquence, période, déclenchement, Auto scale, Run/Single, curseurs, export. |
| Revues | Revues des trois mises à jour de Codex et rapports PDF. |

## Chronologie des commits

Les liens ouvrent chaque commit sur
[github.com/Citroz31/arty-frame-studio](https://github.com/Citroz31/arty-frame-studio).
Les commits « Publish reference firmware » sont produits par le workflow
GitHub Actions à la demande de Claude, après les tests complets.

### 1. Correctifs de fiabilité (3 octobre)

| Commit | Contenu |
| --- | --- |
| [`7fa89e9`](https://github.com/Citroz31/arty-frame-studio/commit/7fa89e95099d3db9968eb42c0e77aa4ec8a2fccf) | Inversion des broches UART corrigée (RX = A9, TX = D10) ; ouverture du port sans reset de la carte (DTR/RTS) ; diagnostics de connexion renforcés. |

### 2. Test des LED, identification et firmware personnalisé (3 octobre)

| Commit | Contenu |
| --- | --- |
| [`adb2114`](https://github.com/Citroz31/arty-frame-studio/commit/adb21141fb95a7fd5af5cbfef270657df5c4e85a) | Commandes LED et INFO (révision 2). Configuration du firmware (horloge du cœur, broches, courant, fronts) avec XDC généré. Compilation locale ou sur GitHub Actions depuis l'interface, avec jeton gardé en mémoire et vérification de l'artefact. |
| [`d065f61`](https://github.com/Citroz31/arty-frame-studio/commit/d065f6161f96c0fe1c97cf671764dae2e7150c0c) | Commande LED enregistrée avant le compteur, placé loin : chemin critique réduit. |
| [`74cec11`](https://github.com/Citroz31/arty-frame-studio/commit/74cec11693502a31ea0b894574ef613313a6e54e) | Balayage des graines de placement nextpnr jusqu'à un routage qui respecte 200 MHz. |
| [`d588999`](https://github.com/Citroz31/arty-frame-studio/commit/d588999403b7e9b4da954b160d5d79e2ed5f63d2) | Place libre de la file de réponses enregistrée avant START/STOP/LED : chemin critique supprimé. |
| [`f73f942`](https://github.com/Citroz31/arty-frame-studio/commit/f73f942) | Firmware de référence révision 2 publié par le workflow (206,14 MHz). |
| [`07946e4`](https://github.com/Citroz31/arty-frame-studio/commit/07946e4f2b5b52bc046589b5ad3caea5843351f5) | Chiffres de la documentation mis à jour. |

### 3. Émission continue jusqu'à Arrêter (4 octobre)

| Commit | Contenu |
| --- | --- |
| [`822f091`](https://github.com/Citroz31/arty-frame-studio/commit/822f09181a31635a31966f5813e7a77808149880) | SEND avec `repeat_count = 0` répète la trame sans fin jusqu'à STOP (révision 3). Interrupteur dans l'interface, `send --continuous / --duration` en ligne de commande, démo et simulation. `last_frame` devient une bascule, ce qui raccourcit le chemin critique. |
| [`44f2a61`](https://github.com/Citroz31/arty-frame-studio/commit/44f2a61) | Firmware révision 3 publié (212,77 MHz). |
| [`fe71456`](https://github.com/Citroz31/arty-frame-studio/commit/fe71456e3639dbf95ed47fc9066e7fe68852577f) | Chiffres de la documentation mis à jour. |

Fusionné dans `main` par la pull request [#1](https://github.com/Citroz31/arty-frame-studio/pull/1).

### 4. CLK libre et marge de timing (4 octobre)

| Commit | Contenu |
| --- | --- |
| [`74c347e`](https://github.com/Citroz31/arty-frame-studio/commit/74c347e907184ec5c53c380f45aec618714acd8e) | CLK libre (révision 4) : générateur d'horloge indépendant, LATCH dès le dernier front descendant, LATCH et pause en périodes entières de CLK. Interrupteur, `--free-clock`, profils schéma 3, tracé des chronogrammes denses. |
| [`2bb4816`](https://github.com/Citroz31/arty-frame-studio/commit/2bb48169e502263fd815ce640d57bdf1e9f9975f) | Fenêtre de mesure du banc carte corrigée (exactement 10 µs). |
| [`20960c7`](https://github.com/Citroz31/arty-frame-studio/commit/20960c7) | Firmware révision 4 publié, à 200,04 MHz : sans marge, et donc remplacé ensuite. |
| [`dbe6803`](https://github.com/Citroz31/arty-frame-studio/commit/dbe680347c195c94bc26d02c145862e264aa5c35) | Les graines de placement sont essayées jusqu'à 3 % de marge ; sinon, la plus rapide est gardée et ses fichiers sont restaurés. |
| [`1cb0653`](https://github.com/Citroz31/arty-frame-studio/commit/1cb0653) | Firmware révision 4 publié à **210,79 MHz** (graine 8), version actuelle. |
| [`7a7142f`](https://github.com/Citroz31/arty-frame-studio/commit/7a7142f0aae5b223e8db9511a69d79f6d8ccb42c) | Chiffres de la documentation mis à jour. |

Fusionné dans `main` par la pull request [#2](https://github.com/Citroz31/arty-frame-studio/pull/2).

### 5. Saisie de trame en binaire (4 octobre)

| Commit | Contenu |
| --- | --- |
| [`86f3856`](https://github.com/Citroz31/arty-frame-studio/commit/86f38568a4a533f2cfec2fec63e23589a94e5eea) | Notation binaire par défaut. Le nombre de bits suit les chiffres saisis, zéros de tête compris, et est limité à 26 avec un message explicite. Les conversions entre notations conservent la longueur. Guide utilisateur et README mis à jour, 15 tests ajoutés. |

### 6. Erreurs du journal Windows et oscilloscope (5 octobre)

| Commit | Contenu |
| --- | --- |
| [`01bb7f0`](https://github.com/Citroz31/arty-frame-studio/commit/01bb7f0f3857c596d8392531cc719fdb660c1cd4) | Sans `toolchain.json` (cas normal sous Windows), les boutons de la chaîne locale sont désactivés avec une explication au lieu d'échouer ; la configuration est lue avant de fermer la liaison UART ; message explicite ; bandeau refermable ; avertissements OneDrive et chemins longs au démarrage. |
| [`e5b0214`](https://github.com/Citroz31/arty-frame-studio/commit/e5b0214d85f3222fe81659dedf238bfdbe0b85a9) | Onglet **Oscilloscope** : pilote SCPI LAN (port 5025) et USB/VISA, oscilloscope simulé, mesures de fréquence et période, déclenchement, calibres avec loupes, Auto scale, préréglage de la trame, Run/Stop/Single, curseurs, export CSV et PNG, avertissements de sonde ; commandes `scope` et `scope-list`. |
| [`e732b0e`](https://github.com/Citroz31/arty-frame-studio/commit/e732b0e2582882063e178fcccd12fae5dacf9e2f) | Documentation : [Oscilloscope](oscilloscope.md), guide utilisateur, guide Windows et README. |
| [`d54f494`](https://github.com/Citroz31/arty-frame-studio/commit/d54f4940bf025d400ebc31a41ac568c4166eb1db) | Test de l'écran corrigé : avec le vrai Flet, les formes du canevas n'ont pas toutes un texte (erreur révélée par la CI). |

## Détail des fonctions ajoutées

### Firmware (RTL)

- **Révision 2** : commande LED (motif sur LD4–LD7 pendant 3 s) et INFO
  (révision, horloge du cœur, capacités, identifiant de build sur six pages).
- **Révision 3** : `repeat_count = 0` signifie une émission continue jusqu'à
  STOP ; le compteur `completed` reboucle à 65 536.
- **Révision 4** : bit 2 des flags pour la CLK libre. Le générateur
  d'horloge suit la même période que la CLK de trame, les deux étant en
  phase pendant les bits.
- **Révision 5** : broche TR statique (3,3 V ou 0 V, JB4 par défaut).
- **Révision 6** : deux domaines d'horloge (UART, paquets, LED et TR à
  100 MHz ; moteur et sorties DDR sur l'horloge du PLL), moteur de trame
  réécrit pour 300 MHz, diviseur d'entrée `DIVCLK_DIVIDE` du PLL.
- **Timing** : quatre chemins critiques enregistrés (entrées ODDR, commande
  LED, place libre de la file de réponses, dernière trame), puis le moteur de
  la révision 6. Fmax routée actuelle : 288,93 MHz pour 200 MHz exigés
  (révision 6, graine 1), contre 211,82 MHz pour la révision 5.

### Application (Python)

- L'émission continue et la CLK libre sont refusées avant l'envoi si le
  firmware n'annonce pas la capacité correspondante, avec un message
  expliquant quoi recharger. Une trame calculée pour une autre horloge du
  cœur est elle aussi refusée.
- L'interface propose : test LED, identité du firmware, interrupteurs
  « Répéter jusqu'à Arrêter » et « CLK libre », personnalisation et
  compilation du firmware, saisie binaire automatique.
- En ligne de commande : `led-test`, `info`, `firmware-config`,
  `remote-build`, `send --continuous --free-clock --duration`.
- Le simulateur et les exports SVG/CSV/VCD couvrent les émissions continues
  et la CLK libre.

### Compilation et publication

- Workflow GitHub `firmware.yml` : compilation à la demande avec une
  configuration personnalisée, ou republication du firmware de référence
  après les tests complets.
- Le firmware fourni reste lié à ses sources : toute modification du RTL ou
  du XDC exige une republication, contrôlée par les tests.

## Vérification

| Niveau | Résultat sur la branche (8 octobre) |
| --- | --- |
| Tests Python | 1 018 tests (CI Linux et Windows natif ; 1 014 dans le job de publication) ; Ruff et mypy |
| Bancs RTL | 6 bancs Icarus : moteur (≈ 2 millions de contrôles par demi-tick), ODDR, protocole avec un cœur plus rapide puis plus lent que le contrôle, SIPO, carte complète par l'UART, UART à 100 MHz |
| Compilation réelle | Firmware révision 6 : 288,93 MHz routés pour 200 MHz exigés ; 48 placements entre 250 et 300 MHz : 221 à 344 MHz |
| Carte réelle | Retour utilisateur (révision 4) : chargement SRAM sous Windows, test LED et DATA à 10 MHz sur JB1 observée au DSOX1202A. Révisions 5 (TR) et 6 (haute fréquence) : à essayer |
| VNA | Pilote PNA, liste d'états et Touchstone vérifiés contre un VNA simulé ; **à essayer sur le N5245B et le P9374A** |
| Oscilloscope | Pilote validé avec l'oscilloscope simulé et des liaisons LAN/VISA simulées ; **à essayer sur le DSOX1202A** |

## Mode mesure, mode VNA et broche TR (6 octobre)

| Commit | Apport |
| --- | --- |
| [`eba8846`](https://github.com/Citroz31/arty-frame-studio/commit/eba88466896c2ef88ecee5842398664ff1a6eb42) | Chargement JTAG à 6 MHz par défaut, repli à 1 MHz si l'IDCODE ne se lit pas |
| [`d1053d1`](https://github.com/Citroz31/arty-frame-studio/commit/d1053d15147438a97943b0716be8bda10718e563) | Onglet Mesure et `arty-frame sweep` : suite de mots, validation après chacun (opérateur, oscilloscope, instrument SCPI) |
| `9d40ebf` | Mode VNA : liste d'états `mot ; TR ; nom`, canal et ports, un fichier Touchstone par état, détection du VNA, VNA simulé ; opcode TR côté application |
| `06aab96` | Broche TR (firmware révision 5) : RTL, bancs, `tr_pin` dans la configuration du firmware, interrupteur Pilotage, `arty-frame tr` |
| `d40072b` | Un balayage VNA abandonné se termine avant tout autre échange avec l'appareil |
| `d0b3c13` | Banc de carte : le compteur de trames périmé après un STOP n'est plus comparé à zéro |
| `4f4b200` | Firmware de référence révision 5 republié par le workflow (211,82 MHz) |

Voir [le mode mesure](mode-mesure.md) et [le mode VNA](mode-vna.md).

## Haute fréquence : moteur révision 6 et planificateur (7-8 octobre)

| Commit | Apport |
| --- | --- |
| [`7ab7829`](https://github.com/Citroz31/arty-frame-studio/commit/7ab78292b1992332e19daf59aeb89d312ca0ce91) | Moteur de trame réécrit (étapes BIT/TAIL, décompteurs à bit de signe, cycle d'armement), deux domaines d'horloge (`engine_link`, `frame_plan`), PLL `DIVCLK_DIVIDE`, planificateur `plan_frame_clock` et `arty-frame clock-plan`, champs d'horloge en MHz et « Adopter cette horloge » dans Pilotage, vérification du PLL dans le FASM, balayage jusqu'à 16 graines, limite absolue de 300 MHz |
| [`6ef6bab`](https://github.com/Citroz31/arty-frame-studio/commit/6ef6bab9acea72633e7dd32c9bfd1aaee801568b) | Firmware de référence révision 6 republié par le workflow (288,93 MHz pour 200 MHz exigés) |

La fréquence maximale atteignable est établie par 48 placements routés :
la logique du moteur passe 300 MHz dans un placement sur trois (jusqu'à
344 MHz), le réseau d'horloge BUFG en vitesse -1 en permettrait 464 ; la
limite absolue appliquée est **300 MHz**. Voir
[la fréquence maximale](hardware.md#fréquence-maximale-de-cette-carte) et
[les mesures](verification.md#moteur-révision-6-et-fréquence-maximale).

## Revues des mises à jour de Codex

| Commit Codex | Avis |
| --- | --- |
| [`54feead`](https://github.com/Citroz31/arty-frame-studio/commit/54feeada3180a09564a4f289b06aaa38b801ee57) | Sain : interface réorganisée, validation des firmwares téléchargés. Une compilation à 150 MHz a confirmé sur les vrais outils la période XDC plus stricte. |
| [`130465a`](https://github.com/Citroz31/arty-frame-studio/commit/130465a18d28f88416d0cae50b0d82529b8a3550) | Sain : émission indéfinie explicite, contrôle des capacités, arrêt CLI plus sûr, aperçus bornés, guide SIPO et banc SIPO. |

## Pistes restantes

1. **Essayer l'onglet Oscilloscope sur le DSOX1202A** en LAN puis en USB, et
   mesurer CLK, DATA et LATCH avec des sondes ×10 et le ressort de masse.
2. **Actualiser `uv.lock`** (`uv lock`) pour PyVISA : PyPI n'était pas
   joignable depuis l'environnement de Claude. `pip` et `start-windows.cmd`
   ne sont pas concernés.
3. **Mesurer à l'oscilloscope la fréquence CLK réellement exploitable** sur
   le Pmod JB : la logique va jusqu'à 300 MHz, la sortie LVCMOS33 et le
   câblage bien moins. Monter progressivement depuis 10 MHz.
4. **Arrêt propre en fin de trame**, **mise à jour du mot sans arrêter CLK**,
   **firmware en mémoire flash**.
5. **Découper `app.py`** (≈ 1 900 lignes) en contrôleurs testables séparément.
