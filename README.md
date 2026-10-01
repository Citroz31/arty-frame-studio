# Arty Frame Studio

Application de bureau **Python / Flet**, en français, pour une **Digilent Arty
A7-100T**, avec pilotage USB/UART, trames série jusqu’à **26 bits**, sorties
**DATA / CLK / LATCH**, simulation et exports de chronogrammes. La compilation
FPGA utilise Yosys, nextpnr-xilinx et Project X-Ray ; la programmation SRAM utilise
openFPGALoader. Vivado n’est pas utilisé par le projet.

Le PC envoie les paramètres par UART ; le FPGA produit les fronts. La fréquence
réalisable est **200 MHz / N**, N entier de 1 à 65535, soit environ 3,052 kHz à
200 MHz. La fréquence réellement sélectionnée est affichée. Les durées latch et
pause sont quantifiées à **2,5 ns**. CLK est émise en rafales, idle bas ; le
destinataire échantillonne DATA au front montant. DATA change au front descendant.

**Le fonctionnement à 200 MHz sur la carte n’a pas été mesuré.** Le moteur utilise
des sorties DDR, mais une fréquence numérique demandée ne garantit pas le timing
du placement/routage, ni l’intégrité d’une liaison Pmod. Voir
[le matériel](docs/hardware.md) et [la chaîne FPGA](docs/toolchain.md).
Le dépôt contient le RTL et les commandes de compilation ; aucun bitstream
100T routé et vérifié sur carte n’est fourni.

![Interface de simulation](docs/images/chronogramme-interface.png)

## Démarrage

Python 3.11 ou plus récent. Depuis ce dossier :

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
arty-frame-studio
```

Sous Windows, activer avec `.venv\Scripts\activate`. L’interface et le port série
fonctionnent sous Windows/macOS/Linux ; la chaîne FPGA décrite est destinée à
Linux. Un environnement de bureau est nécessaire pour Flet. Aucun compilateur
FPGA n’est requis pour utiliser la simulation et le mode démonstration.

Avec `uv`, le fichier `uv.lock` permet une installation figée :
`uv sync --extra dev --frozen`, puis `uv run arty-frame-studio`.
Conserver le dossier du projet pour la compilation FPGA. Depuis une installation
wheel séparée, indiquer son emplacement avec `ARTY_FRAME_PROJECT` pour l’interface
ou `--project-root` pour les commandes FPGA de la CLI.

## Utilisation

1. Ouvrir l’application et sélectionner le mode démonstration pour découvrir le
   pilotage sans carte. Saisir la valeur en binaire, hexadécimal ou décimal,
   choisir 1 à 26 bits et régler la fréquence.
2. Définir l’ordre MSB/LSB, la polarité du latch, sa durée, la pause après trame et
   le nombre de répétitions. Le FPGA accepte une seule séquence à la fois ; STOP
   interrompt celle qui est active.
3. Afficher le chronogramme idéal et exporter en SVG, CSV ou VCD (GTKWave). Le
   nombre de répétitions affichées est limité ; cette limite est indiquée sur
   le graphique et s’applique aussi aux exports.
4. Pour la carte réelle, installer les outils et la base de données FPGA en
   suivant [docs/toolchain.md](docs/toolchain.md), puis renseigner `toolchain.json`.
   Compiler et charger le bitstream depuis l’onglet FPGA.
5. Brancher le port USB de l’Arty, choisir son port série, connecter, puis envoyer
   une trame. La connexion vérifie la réponse du firmware fourni : un bitstream
   UART compatible doit déjà être chargé dans la carte.

La programmation proposée charge la **SRAM volatile** : il faut reprogrammer
après une coupure d’alimentation. Une programmation de la flash n’est pas effectuée.
Les profils sauvegardent les paramètres dans un JSON versionné.

## Vérification

```bash
pytest
ruff check src tests
ruff format --check src tests
mypy src/arty_frame_studio
python firmware/sim/run_tests.py
```

La dernière commande nécessite Icarus Verilog. **146 tests Python et cinq bancs
RTL passent**, avec vérification des types et du formatage. Le projet contient des tests du
protocole et du transport, des chronogrammes et du moteur RTL. Ces tests numériques
ne remplacent pas une mesure à l’oscilloscope sur la carte.

Structure : `src/arty_frame_studio/` contient l’application et la CLI,
`firmware/` le RTL et le brochage, `tests/` les tests, `examples/` les profils,
et `docs/` le protocole et la procédure de compilation.

## Ligne de commande

```bash
arty-frame simulate --profile examples/frame_26bits.json --output exports/trame
arty-frame send --profile examples/frame_26bits.json --demo --wait
arty-frame ports
arty-frame send --profile examples/frame_26bits.json --port /dev/ttyUSB1 --wait
arty-frame status --port /dev/ttyUSB1
arty-frame stop --port /dev/ttyUSB1
arty-frame doctor --toolchain toolchain.json
arty-frame build --toolchain toolchain.json
arty-frame program --toolchain toolchain.json
```

Exemple de sortie idéale : [chronogramme SVG](examples/chronogramme.svg).
Les broches par défaut sont **JB1 DATA, JB2 CLK, JB3 LATCH**.
Le firmware produit une séquence finie localement ; la vitesse UART ne limite
pas la fréquence de ses fronts. La cadence de nouvelles commandes reste
limitée par la liaison UART.

Licence MIT.
