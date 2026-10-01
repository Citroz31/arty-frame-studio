# Chaîne FPGA libre, sans Vivado

La cible est exclusivement l'**Arty A7-100T, xc7a100tcsg324-1**. Yosys réalise la
synthèse, le fork `gatecat/nextpnr-xilinx` le placement/routage et la production
FASM, Project X-Ray la conversion FASM → frames → `.bit`, et openFPGALoader le
chargement JTAG en SRAM. Le chemin xc7 de ce fork est décrit par son auteur comme
ne nécessitant pas Vivado. Le `nextpnr` générique des distributions, une chipdb
35T, et le chemin UltraScale/RapidWright ne sont pas des substituts.

Cette chaîne reste expérimentale. Les tests logiciels du projet vérifient les
arguments, les erreurs et la protection contre les sorties périmées. Ils ne
constituent pas une compilation routée sur 100T ni une validation sur carte. Les
versions, la couverture des primitives et les données de timing doivent être
vérifiées sur votre installation. Aucune fréquence physique de 200 MHz n'est
certifiée par l'application.

## Préparation sous Linux

Les installations suivantes sont distinctes de l'environnement Python/Flet de
l'application. La base xc7 peut occuper plusieurs Go : elle n'est pas distribuée
avec le projet. Prévoir suffisamment de RAM et d'espace pour sa génération.

```bash
sudo apt update
sudo apt install git cmake ninja-build build-essential pkg-config \
  libboost-all-dev libeigen3-dev libffi-dev libreadline-dev \
  libgflags-dev libyaml-cpp-dev zlib1g-dev \
  libusb-1.0-0-dev libftdi1-dev python3-dev python3-venv \
  yosys openfpgaloader
mkdir -p "$HOME/arty-tools"
```

Si `openfpgaloader` n'est pas proposé par la distribution, le compiler selon la
documentation officielle. Installer ses règles udev, reconnecter l'USB et garder
les privilèges utilisateur ordinaires pour lancer l'interface. Le câble USB de
l'Arty dessert à la fois JTAG et UART via le FT2232.

La méthode de reproductibilité est de figer **ensemble** le commit nextpnr et
ses sous-modules, le commit Project X-Ray, et la version Yosys, puis de générer
la chipdb avec ce même nextpnr. Ne mélangez pas un `.bin` trouvé ailleurs avec
un binaire nextpnr d'un autre commit : le format interne peut évoluer.

Les commandes ci-dessous suivent les procédures amont et sont un guide de
construction, pas une affirmation qu'un ensemble de versions a été validé sur
une carte. Le hash Project X-Ray correspond à la source consultée pendant
le développement. Les deux commits sont figés ci-dessous ; enregistrez les
versions effectivement installées et conservez les sous-modules.

```bash
export ARTY_TOOLS="$HOME/arty-tools"
# Utiliser des commits complets plutôt que des branches mobiles.
export ARTY_NEXTPNR_REV="8f178fc6a6d4dfbc57bef66c3ccff34d558047d5"
export ARTY_XRAY_REV="c9f02d8576042325425824647ab5555b1bc77833"

git clone https://github.com/gatecat/nextpnr-xilinx.git "$ARTY_TOOLS/nextpnr-xilinx"
git -C "$ARTY_TOOLS/nextpnr-xilinx" checkout "$ARTY_NEXTPNR_REV"
git -C "$ARTY_TOOLS/nextpnr-xilinx" submodule update --init --recursive

git clone https://github.com/f4pga/prjxray.git "$ARTY_TOOLS/prjxray"
git -C "$ARTY_TOOLS/prjxray" checkout "$ARTY_XRAY_REV"
git -C "$ARTY_TOOLS/prjxray" submodule update --init --recursive

python3 -m venv "$ARTY_TOOLS/venv"
"$ARTY_TOOLS/venv/bin/python" -m pip install --upgrade pip
"$ARTY_TOOLS/venv/bin/python" -m pip install -r "$ARTY_TOOLS/prjxray/requirements.txt"
"$ARTY_TOOLS/venv/bin/python" -m pip install -e "$ARTY_TOOLS/prjxray"

cmake -S "$ARTY_TOOLS/prjxray" -B "$ARTY_TOOLS/prjxray/build"
cmake --build "$ARTY_TOOLS/prjxray/build" --target xc7frames2bit -j2
cmake -S "$ARTY_TOOLS/nextpnr-xilinx" -B "$ARTY_TOOLS/nextpnr-xilinx" -DARCH=xilinx
cmake --build "$ARTY_TOOLS/nextpnr-xilinx" -j2

cd "$ARTY_TOOLS/nextpnr-xilinx"
"$ARTY_TOOLS/venv/bin/python" xilinx/python/bbaexport.py \
  --device xc7a100tcsg324-1 --bba xilinx/xc7a100tcsg324-1.bba \
  --xray xilinx/external/prjxray-db/artix7 \
  --metadata xilinx/external/nextpnr-xilinx-meta/artix7
./bba/bbasm --l xilinx/xc7a100tcsg324-1.bba xilinx/xc7a100tcsg324-1.bin

git rev-parse HEAD > "$ARTY_TOOLS/nextpnr-revision.txt"
git submodule status --recursive > "$ARTY_TOOLS/nextpnr-submodules.txt"
git -C "$ARTY_TOOLS/prjxray" rev-parse HEAD > "$ARTY_TOOLS/prjxray-revision.txt"
"$ARTY_TOOLS/venv/bin/python" -m pip freeze > "$ARTY_TOOLS/python-requirements.lock"
yosys -V > "$ARTY_TOOLS/yosys-version.txt"
```

Le script `bbaexport.py` utilise la base et les métadonnées du fork nextpnr.
Contrôler ses options avec `--help` si le commit choisi modifie leur emplacement.
Le répertoire de base Project X-Ray doit être le dossier `artix7`, contenant
`xc7a100tcsg324-1/part.yaml`, avec ses fichiers tilegrid/segbits et métadonnées.
La génération des fichiers de configuration utilise une base préexistante :
il ne faut pas lancer les fuzzers Project X-Ray, qui servent à caractériser le
composant et peuvent dépendre d'outils propriétaires.

Ne sourcez pas les anciens scripts `utils/environment.sh` de Project X-Ray
qui exigent une installation Vivado. Les commandes de l'application appellent
directement `fasm2frames.py` et `xc7frames2bit`.

## Configuration et utilisation

Depuis la racine de ce projet :

```bash
cp examples/toolchain.example.json toolchain.json
# Éditer toolchain.json : remplacer /opt/arty-tools par vos chemins absolus.
arty-frame doctor --toolchain toolchain.json
arty-frame build --toolchain toolchain.json
arty-frame program --toolchain toolchain.json --bitstream build/arty_frame.bit
```

Ces opérations sont aussi disponibles dans l'onglet FPGA de l'interface Flet.
Les chemins `chipdb`, `prjxray_db`, `build_dir` sont résolus relativement au
fichier JSON. Les exécutables et arguments de commandes sont transmis
littéralement : utilisez des chemins absolus pour les scripts et interpréteurs.
`fasm2frames` peut être un tableau `["/chemin/python", "/chemin/fasm2frames.py"]`.
`openfpgaloader` contient uniquement l'exécutable, sans options supplémentaires.
Le diagnostic teste la présence des outils/fichiers, pas leur compatibilité
complète ni la présence de la carte.

La compilation exécute successivement :

```text
yosys -p 'read_verilog ...; synth_xilinx -family xc7 -flatten -nodram -top arty_top; write_json ...'
nextpnr-xilinx --chipdb ... --xdc ... --json ... --fasm ... --freq 200
python fasm2frames.py --db-root .../artix7 --part xc7a100tcsg324-1 design.fasm
xc7frames2bit --part_file .../part.yaml --part_name xc7a100tcsg324-1 \
              --frm_file design.frames --output_file design.bit
openFPGALoader -b arty_a7_100t build/arty_frame.bit
```

`fasm2frames` écrit les frames sur stdout ; ce flux est conservé séparément des
diagnostics stderr. Aucune commande ne passe par un shell. `--timing-allow-fail`
est interdit. Les commandes et leurs sorties figurent dans `build/build.log`
et `build/program.log`. Chaque compilation utilise un dossier neuf dans
`build/runs/`. Le chargement porte uniquement sur la SRAM ; aucune option de
flash persistante n'est appelée. Après coupure de courant, charger à nouveau.

`-flatten` supprime la hiérarchie avant export JSON. `-nodram` évite les
primitives de petite RAM distribuée que ce fork xc7 ne traite pas toutes.
Le générateur d'horloge utilise `PLLE2_BASE`, couvert par le flux FASM du fork,
avec `COMPENSATION="INTERNAL"`, mode pris en charge par son writer FASM,
un VCO à 1 GHz (100 MHz × 10) et une sortie à 200 MHz (÷ 5).

## Horloge et portée des rapports de timing

Le FPGA produit une horloge interne de 200 MHz à partir de l'oscillateur E3 de
100 MHz. Les ODDR génèrent les demi-cycles de 2,5 ns. La fréquence sélectionnée
dans l'application vaut 200 MHz / N, N entier de 1 à 65535 ; le Python et l'USB
ne produisent aucun front GPIO.

Le parseur XDC de `gatecat/nextpnr-xilinx` prend en charge `create_clock` sur
`get_ports` et `get_nets`. Il ignore `create_generated_clock`, les contraintes
de délai d'entrée/sortie et les commandes Tcl SDC générales. Le XDC fourni
impose donc explicitement **10 ns sur clk100 et 5 ns sur le net core_clock**,
avec la syntaxe `[get_nets core_clock]` sans accolades. Le `--freq 200` reste
une cible par défaut. La compilation exige dans la sortie nextpnr un rapport
`Max frequency for clock 'core_clock': ... (PASS at 200.00 MHz)` ou une
contrainte plus stricte, Fmax ≥ 200 MHz, et refuse tout rapport FAIL. Si le net
est renommé ou si ce rapport manque, elle bloque plutôt que de fabriquer un
bitstream supposé valide.

**Cette vérification ne couvre pas toute la sortie DDR.** Le code de timing
xc7 de ce fork modélise surtout les registres internes ; les endpoints ODDR
et la liaison GPIO externe ne bénéficient pas d'une analyse complète. Le
rapport PASS est une condition nécessaire pour poursuivre, pas une preuve de
setup/hold à l'entrée du périphérique distant. Les phases CLK/DATA/LATCH,
la charge des broches, les pistes, les câbles et le récepteur doivent être
mesurés et validés. Un chronogramme idéal ne fournit pas ces mesures.

Un build réussi crée `successful-build.json` avec les hashes des sources,
contraintes, configuration et bitstream. Tout nouveau build révoque ce reçu,
même s'il échoue avant la synthèse. Une sortie vide, une erreur de commande,
un timing manquant/raté, ou des sources modifiées interdisent la programmation
depuis l'application. Un ancien `.bit` peut rester pour inspection, sans être
autorisé. Le verrou `.toolchain.lock` exclut compilation et programmation
simultanées ; après un arrêt brutal, supprimer le verrou seulement après avoir
vérifié qu'aucun processus de compilation ou de programmation ne tourne.

## Sources amont

- [nextpnr-xilinx : flux xc7, primitives et génération chipdb](https://github.com/gatecat/nextpnr-xilinx)
- [Parseur XDC réellement utilisé](https://github.com/gatecat/nextpnr-xilinx/blob/master/xilinx/xdc.cc)
- [Modèle de timing de l'architecture](https://github.com/gatecat/nextpnr-xilinx/blob/master/xilinx/arch.cc)
- [Yosys synth_xilinx](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/synth_xilinx.html)
- [Project X-Ray et outils de conversion](https://github.com/f4pga/prjxray)
- [Base Project X-Ray](https://github.com/f4pga/prjxray-db)
- [openFPGALoader](https://github.com/trabucayre/openFPGALoader)

Brochage et limites physiques : [hardware.md](hardware.md).
