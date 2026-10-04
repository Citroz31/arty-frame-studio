# Récapitulatif des apports de Claude

Ce document résume tout ce que Claude a apporté au projet du 3 au 4 octobre
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
| Firmware à la demande | Choix de l'horloge du cœur (32 valeurs, 50–200 MHz) et des broches DATA/CLK/LATCH, compilation locale ou sur GitHub Actions depuis l'interface. |
| Émission continue | Répétition de la trame jusqu'à Arrêter (firmware révision 3). |
| CLK libre | Horloge sans interruption pendant LATCH et la pause (firmware révision 4). |
| Timing 200 MHz | Chemins critiques réenregistrés, balayage des graines de placement avec marge de 3 %. |
| Saisie de trame | Binaire par défaut, nombre de bits calculé automatiquement, 26 bits maximum. |
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

## Détail des fonctions ajoutées

### Firmware (RTL)

- **Révision 2** : commande LED (motif sur LD4–LD7 pendant 3 s) et INFO
  (révision, horloge du cœur, capacités, identifiant de build sur six pages).
- **Révision 3** : `repeat_count = 0` signifie une émission continue jusqu'à
  STOP ; le compteur `completed` reboucle à 65 536.
- **Révision 4** : bit 2 des flags pour la CLK libre. Le générateur
  d'horloge suit la même période que la CLK de trame, les deux étant en
  phase pendant les bits.
- **Timing** : quatre chemins critiques enregistrés (entrées ODDR, commande
  LED, place libre de la file de réponses, dernière trame). Fmax routée
  actuelle : 210,79 MHz pour 200 MHz exigés.

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

| Niveau | Résultat sur le dernier commit (`86f3856`) |
| --- | --- |
| Tests Python | 501 tests, Linux et Windows natif ; Ruff et mypy |
| Bancs RTL | 6 bancs Icarus : moteur (≈ 2 millions de contrôles par demi-tick), ODDR, protocole, SIPO, carte complète par l'UART, UART à 200 MHz |
| Compilation réelle | Firmware révision 4 : 210,79 MHz routés pour 200 MHz exigés |
| Carte réelle | **Non testé** |

## Revues des mises à jour de Codex

| Commit Codex | Avis |
| --- | --- |
| [`54feead`](https://github.com/Citroz31/arty-frame-studio/commit/54feeada3180a09564a4f289b06aaa38b801ee57) | Sain : interface réorganisée, validation des firmwares téléchargés. Une compilation à 150 MHz a confirmé sur les vrais outils la période XDC plus stricte. |
| [`130465a`](https://github.com/Citroz31/arty-frame-studio/commit/130465a18d28f88416d0cae50b0d82529b8a3550) | Sain : émission indéfinie explicite, contrôle des capacités, arrêt CLI plus sûr, aperçus bornés, guide SIPO et banc SIPO. |

## Pistes restantes

1. **Valider sur la carte** : PING, INFO (« révision 4 »), test LED, puis
   CLK, DATA et LATCH à l'oscilloscope, en commençant à basse fréquence.
2. **Refaire les captures d'écran** de la documentation, qui montrent encore
   l'ancienne saisie en hexadécimal.
3. **Rendre la marge à 200 MHz indépendante du placement** : seules 3 graines
   sur 8 passent. Enregistrer un cycle plus tôt la décision de fin de trame.
4. **Arrêt propre en fin de trame**, **mise à jour du mot sans arrêter CLK**,
   **firmware en mémoire flash**.
5. **Découper `app.py`** (≈ 1 900 lignes) en contrôleurs testables séparément.
