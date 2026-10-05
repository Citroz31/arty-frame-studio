# Chaîne FPGA libre et firmware précompilé

La cible est l'**Arty A7-100T, xc7a100tcsg324-1**. Yosys réalise la synthèse,
openXC7/nextpnr le placement/routage et la production FASM, puis Project X-Ray
convertit FASM → frames → `.bit`. Aucun Vivado n'est appelé.

Pour utiliser la carte **sous Windows**, un fichier précompilé permet de
charger le FPGA depuis Flet avec le pilote FTDI existant. Aucun compilateur
FPGA, WSL ou Linux n'est requis sur ce PC. Voir [le guide Windows](windows.md).
Pour modifier l'horloge ou les broches, l'application peut aussi installer une
chaîne libre **Windows native** et compiler sur ce même PC, sans WSL, Linux,
Vivado ni droits administrateur. GitHub Actions reste disponible.

Le [firmware précompilé](../firmware/prebuilt/arty_frame.bit) a été produit
avec cette chaîne : synthèse, placement/routage et contrôle de timing réussis
à **200 MHz**, avec une Fmax après routage de **210,79 MHz** (graine 8). Les sorties
physiques et le chargement de ce firmware n'ont pas encore été testés sur carte.

## Outils épinglés et provenance

Le script [bootstrap-fpga-tools.sh](../scripts/bootstrap-fpga-tools.sh)
télécharge des archives précompilées, vérifie leur SHA256, extrait les outils,
contrôle la chipdb 100T et produit `build/toolchain.ci.json` ainsi que
`build/tool-versions.json`. Cette préparation a été exécutée dans
l'environnement de développement.

| Archive | Version utilisée | SHA256 |
| --- | --- | --- |
| [OSS CAD Suite Linux x64](https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-03-24/oss-cad-suite-linux-x64-20260324.tgz) | 2026-03-24 ; Yosys `0.63+173`, commit `66306a8ca` | `69f5b5f306c92ea73322cda570242a7a1e4c49e416288750a56cd0feab49c155` |
| [openXC7 Linux x86-64](https://github.com/cavearr/toolchain-openxc7-releases/releases/download/2026-09-30/openxc7-toolchain-linux-x86-64-20260930.tgz) | 2026-09-30 ; nextpnr `c68c1358` | `2a4128908d848423005933ec2c5c2960213368f4b4b9a65287a7d898357335da` |

Le bundle openXC7 inclut les convertisseurs, la base Project X-Ray Artix-7
et une chipdb cohérente avec son exécutable :

- nextpnr : `c68c13582e972292c86a5025140d52e713384cbc` ;
- base Project X-Ray : `a90f27c1caefee5276f47440f4c730b50519a86f` ;
- identifiant chipdb : `66c7425d4ef246f9`.

Ces archives Linux sont exécutées par l'environnement de compilation distant.
L'installation locale Windows utilise la version Windows de la même OSS CAD
Suite et le
[bundle openXC7 Windows natif](https://github.com/cavearr/toolchain-openxc7-releases/releases/download/2026-09-30/openxc7-toolchain-windows-amd64-20260930.tgz)
du 30 septembre 2026. Les versions et SHA256 sont épinglés dans
[local_tools.py](../src/arty_frame_studio/local_tools.py) ; chaque archive est
vérifiée avant extraction ou exécution.

## Compiler sur le PC Windows

Dans l'application, **Installer les outils Windows locaux** prépare les deux
bundles portables et écrit `toolchain.json` à la racine du projet. Les outils
sont rangés dans `%LOCALAPPDATA%\ArtyFrameStudio\fpga-tools`, à l'extérieur du
dossier OneDrive du projet. Le téléchargement ne démarre que lors de cette
installation ou d'une préparation demandant une compilation si `toolchain.json`
est absent ; les compilations suivantes réutilisent les outils. Les deux
archives représentent environ 447 Mo ; prévoir 4 Go libres pour les archives
et les fichiers extraits. Les builds sont conservés séparément dans
`%LOCALAPPDATA%\ArtyFrameStudio\builds\<identifiant-du-projet>`.

Depuis **Pilotage**, **Préparer le firmware depuis Pilotage** réutilise le
firmware compatible ou compile les réglages choisis sur ce PC. La compilation
utilise Yosys ABC9, le backend nextpnr `himbaechel`, la graine 8 et une marge de
timing recherchée de 3 %. Le `.bit` n'est disponible pour programmation que si
le timing à l'horloge choisie, les resets ODDR et le format/cible du fichier
sont validés. La marge est un objectif de placement ; un résultat qui atteint
l'horloge demandée reste accepté même si sa marge est inférieure à 3 %.
Les paramètres UART sont enregistrés dans `profiles/pilotage-frame.json` et
la préparation dans `profiles/pilotage-preparation.json`. Aucun chargement
du FPGA ni envoi de trame n'est lancé par cette préparation.

Le convertisseur `fasm2frames` est exécuté par
`oss-cad-suite/lib/python3.exe` avec les modules Python fournis par openXC7.
Les dossiers des exécutables, DLL et modules sont ajoutés à `PATH` et
`PYTHONPATH` uniquement dans les processus enfants. L'environnement Python du
PC et celui de l'application restent inchangés. Les convertisseurs et
`nextpnr-xilinx.exe` proviennent du bundle Windows, sans script Bash.

La même installation est accessible en ligne de commande :

```bat
.venv\Scripts\arty-frame.exe install-fpga-tools --project-root .
.venv\Scripts\arty-frame.exe doctor --toolchain toolchain.json
.venv\Scripts\arty-frame.exe build --toolchain toolchain.json
```

`--tools-dir DOSSIER` permet de choisir le dossier des outils et
`--config JSON` le fichier de configuration. Le chargement SRAM Windows
utilise le pilote FTDI D2XX existant ; il ne nécessite pas openFPGALoader.

## Compilation sur GitHub Actions

Le workflow [Build Arty A7-100T firmware](../.github/workflows/firmware.yml)
installe les outils épinglés, exécute les simulations RTL, compile le firmware,
exige le timing 200 MHz et contrôle le fichier `.bit`. Dans GitHub, ouvrir
**Actions → Build Arty A7-100T firmware**, puis une exécution réussie.
L'archive **arty-a7-100t-firmware-…** n'est créée que si tous ces contrôles
réussissent. Les journaux restent disponibles séparément en cas d'échec.

Le fichier publié dans `firmware/prebuilt/` est accompagné de :

| Fichier | Contenu |
| --- | --- |
| [arty_frame.bit](../firmware/prebuilt/arty_frame.bit) | Configuration SRAM pour le 100T |
| [firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json) | Sources, cible, versions des outils, hash du fichier et état de validation matérielle |
| [successful-build.json](../firmware/prebuilt/successful-build.json) | Reçu du build et hashes des entrées |
| [build.log](../firmware/prebuilt/build.log) | Commandes et résultats, dont le rapport de fréquence après routage |
| [timing.json](../firmware/prebuilt/timing.json) | Rapport de timing nextpnr |

Le build de référence utilise **ABC9**, la graine nextpnr **8** (seule des
graines 1 à 8 à dépasser 206 MHz) et la cible `xc7a100tcsg324-1`. Le
rapport final indique **210,79258728027344 MHz** sur `core_clock`, avec
**PASS at 200.00 MHz**. Le `.bit` contient un IDCODE
`0x03631093`, compatible avec l'IDCODE `0x13631093` rapporté sur la carte
(révision différente). Les trois resets ODDR R→SR ont été vérifiés dans le
netlist routé. Le fichier mesure **3 825 995 octets** et son SHA256 est :

```text
02c208aa8cbe79599f605e2f14d667a8f4a77361e56637832449e359f2edb57e
```

## Reproduire un build dans un environnement de développement

Le firmware distribué provient du commit `dbe6803` et du
[run GitHub Actions 37210822783](https://github.com/Citroz31/arty-frame-studio/actions/runs/37210822783),
publié par son job `publish`. Il ajoute au brochage UART corrigé, aux
entrées ODDR enregistrées et aux commandes LED et INFO l'émission continue
jusqu'à STOP (révision 3) et la CLK libre pendant LATCH et pause (révision 4).
`arty-frame firmware-check` contrôle le fichier, les sources et le rapport
de timing avant son utilisation, sans outil FPGA ni matériel raccordé.

Depuis la racine du projet, dans un environnement de développement Linux
x86-64 disposant de Python 3.11+, Bash, curl, tar et sha256sum :

```bash
python -m pip install -e .
bash scripts/bootstrap-fpga-tools.sh
python -m arty_frame_studio.cli build --toolchain build/toolchain.ci.json
```

Aucune commande privilégiée, construction C++ ou caractérisation de puce
n'est demandée par ce script. Les outils et les données occupent plusieurs
Go. Le script de préparation Linux est destiné au développement et à la CI ;
l'installation Windows ci-dessus est indépendante de ce script.

La configuration accepte `nextpnr_backend` avec deux valeurs :

| Backend | Options de placement/routage |
| --- | --- |
| `himbaechel` | Bundle openXC7 utilisé : `--device xc7a100tcsg324-1`, `-o xdc=…`, `-o fasm=…`, `--report timing.json`, `--write routed.json` |
| `classic` | Ancien fork gatecat/nextpnr-xilinx : `--xdc …`, `--fasm …` |

`classic` reste la valeur par défaut pour les configurations historiques.
Les formats chipdb et les options des deux backends ne sont pas
interchangeables. Le bootstrap configure explicitement `himbaechel`.
Le `nextpnr` générique d'une distribution ne suffit pas : il faut le backend
xc7, sa chipdb 100T et la base correspondante.

Le mapping Yosys utilise **ABC9 par défaut**, avec `synth_xilinx -abc9`,
configurable par `"yosys_mapping": "abc9"`. Le choix historique
`"yosys_mapping": "abc"` reste disponible ; il doit satisfaire le même
contrôle de timing avant toute production de bitstream.

Les chemins de données et d'exécutables sont résolus relativement au fichier
JSON ; les noms simples sont cherchés dans le PATH. `tool_dirs` et
`python_path` sont des listes de dossiers ajoutés au PATH et au PYTHONPATH des
processus enfants ; leurs chemins relatifs suivent la même règle. Les commandes sont
lancées sans shell. Un convertisseur Python peut être déclaré comme un tableau
`["chemin/python", "chemin/fasm2frames.py"]`. Les exemples
[historique](../examples/toolchain.example.json) et
[Windows](../examples/toolchain.windows.example.json) décrivent des chemins
à adapter ; ils ne distribuent aucun exécutable.

`doctor` vérifie les exécutables et les fichiers, sans garantir leur
compatibilité. L'absence d'openFPGALoader n'empêche pas la compilation ni le
chargement Windows D2XX ; cet outil concerne uniquement le flux externe de
programmation. Le flux natif utilise :

```bat
.venv\Scripts\arty-frame.exe jtag-program --bitstream firmware\prebuilt\arty_frame.bit
```

## Firmware personnalisé depuis l'interface

L'onglet **FPGA → Personnaliser le firmware** produit une configuration validée
(`FirmwareBuildConfig`) : horloge du cœur, broches DATA/CLK/LATCH, courant et
fronts. La configuration par défaut reproduit exactement le firmware de
référence et le XDC du dépôt, qu'un test compare octet par octet.

Pour une autre configuration, le build écrit un XDC généré dans son dossier
`<build_dir>/runs/<id>/`, passe `CORE_HZ`, `PLL_MULT`, `PLL_OUT_DIV` et `BUILD_ID`
à `arty_top` par `chparam` de Yosys, demande `--freq` à l'horloge choisie et
exige le timing à cette horloge. Les sources du dépôt ne sont pas modifiées.
Le reçu `successful-build.json` mémorise la configuration ; `program`
vérifie la même configuration. Une incohérence entre `CORE_HZ` et le PLL
arrête l'élaboration du RTL.

Préparer ou compiler :

| Bouton | Où | Prérequis |
| --- | --- | --- |
| **Préparer le firmware depuis Pilotage** | ce PC Windows | firmware compatible, ou installation des outils pour un nouveau build |
| **Compiler localement** | ce PC Windows | outils portables installés, `toolchain.json` |
| **Compiler sur GitHub** | GitHub Actions, outils épinglés | jeton GitHub, dépôt avec ce workflow |

La compilation GitHub déclenche `firmware.yml` avec la configuration en
entrée, suit l'exécution, télécharge l'artefact dans `builds/` et vérifie le
`.bit` (conteneur, IDCODE, SHA256 du manifeste, configuration identique,
Fmax au moins égale à l'horloge). Le chemin est ensuite proposé à
« Charger le .bit sous Windows ». Une exécution complète dure environ trois
minutes. En ligne de commande :

```bash
arty-frame firmware-config --core-mhz 150 --clock JB1 --data JB3 --latch JB7 --output fw.json
arty-frame build --toolchain toolchain.json --firmware-config fw.json     # local
ARTY_GITHUB_TOKEN=… arty-frame remote-build --firmware-config fw.json      # GitHub
```

Le jeton est un **fine-grained personal access token** limité au dépôt, avec
la permission **Actions : Read and write** (GitHub → Settings → Developer
settings → Personal access tokens). L'application le garde en mémoire, le lit
éventuellement dans `ARTY_GITHUB_TOKEN`, et ne l'envoie qu'à `api.github.com`.
Pour un fork, activer Actions sur le fork et y indiquer son dépôt.

Le workflow reçoit la configuration par variable d'environnement, jamais
interpolée dans un script, et la valide avec le même code Python. Chaque
demande porte un identifiant qui nomme l'exécution et son artefact.

### Publier le firmware de référence

`firmware/prebuilt/` doit correspondre exactement aux sources RTL et XDC :
les tests le vérifient. Après une modification du RTL, lancer le workflow à
la main sur la branche avec **publish_prebuilt** coché (configuration vide).
Après le build, un second job remplace `firmware/prebuilt/`, exécute toute la
suite Python sur ce nouveau contenu, puis le committe sur la branche. Il
refuse de publier si la branche a avancé pendant le build. Ce commit est fait
avec le jeton du workflow : relancer ensuite les vérifications Python/RTL
sur la branche.

## Adaptations des primitives et du mapping

Le PLL utilise **`PLLE2_ADV`**, dont le paramètre
`COMPENSATION="INTERNAL"` est défini, avec entrée 100 MHz, VCO 1 GHz et sortie
200 MHz. `PLLE2_BASE` n'expose pas ce paramètre ; la synthèse réelle a permis
de corriger cette erreur que le précédent modèle de simulation acceptait.

Les trois ODDR conservent leur reset asynchrone **R**. Le packer himbaechel
`c68c1358` tente de réunir R et S sur la même entrée physique SR. Le projet
produit donc une copie du JSON Yosys où seules les connexions **S constantes
à zéro** sont retirées pour ces ODDR. Le JSON d'origine est conservé ; toute
configuration différente est refusée. Après routage, le build vérifie que
DATA, CLK et LATCH conservent le reset R sur leur port physique SR, avec
reset à zéro. Il ne remplace pas ce reset par un masquage des données.

Avec la version Yosys épinglée, `scratchpad -set abc.exe yosys-abc` sélectionne
l'invocation ABC par lot. Cela évite un blocage de son mode interactif lié
aux longs chemins temporaires et à Readline. Les logs conservent la commande
exacte. `-flatten` supprime la hiérarchie et `-nodram` évite les petites RAM
distribuées insuffisamment couvertes par ce flux.

## Timing et portée de la validation

Le XDC impose **10 ns sur clk100 et 5 ns sur core_clock** ; nextpnr reçoit
également `--freq 200`. Le build exige le rapport final après routage
`Max frequency for clock 'core_clock': … (PASS at 200.00 MHz)`, avec Fmax
au moins 200 MHz et une contrainte au moins aussi stricte. Un firmware
personnalisé applique la même règle à son horloge de cœur. Un rapport de
placement provisoire n'est pas une preuve de fermeture du timing.
`--timing-allow-fail` est interdit. Un rapport final absent ou en échec
bloque la conversion en bitstream.

Les chemins du cœur sont proches de 5 ns : le seul placement fait varier la
Fmax routée d'environ 15 % (182 à 220 MHz observés). Le build essaie donc les
graines de placement de `nextpnr_seeds` (par défaut 1 à 8) et s'arrête à la
première qui dépasse l'exigence de `timing_margin` (par défaut 0.03, soit
206 MHz pour 200 MHz). Sinon, il garde la graine **la plus rapide** parmi
celles qui respectent le timing : ses fichiers FASM, `timing.json` et
`routed.json` sont restaurés et `build.log` indique la graine retenue. Un
échec de timing passe à la graine suivante, toute autre erreur de nextpnr
arrête le build. La graine retenue figure dans le reçu et dans le manifeste.
Le timing exigé reste le même : la graine ne change que le placement, jamais
la contrainte ; `timing_margin` à 0 reprend la première graine qui passe.
La configuration générée par l'installeur Windows utilise uniquement la
graine 8, déjà retenue pour le firmware de référence ; `nextpnr_seeds` reste
modifiable dans `toolchain.json` pour essayer d'autres placements.

Avant chaque essai, les sorties de l'essai précédent sont supprimées ; les
copies du meilleur résultat sont conservées séparément. Une sortie FASM,
un rapport de timing ou un netlist routé manquant bloque le build même si
nextpnr annonce un PASS : aucun fichier d'une autre graine ne peut compléter
un essai partiel. Avec le backend himbaechel, les trois fichiers restaurés
proviennent donc du même essai que la graine indiquée dans le reçu. Le journal
conserve tous les essais, y compris ceux qui échouent après le meilleur PASS :
consulter sa ligne « graine … retenue » et le rapport `timing.json` restauré,
plutôt que supposer que la dernière ligne de fréquence est celle retenue.

Ce contrôle couvre les chemins modélisés par nextpnr. Il **ne certifie pas
l'interface DDR ni la liaison Pmod externe** : fronts, skew, câbles et marges
setup/hold du récepteur doivent être mesurés. Le firmware n'a pas été testé
sur carte ici. Le chronogramme représente le comportement idéal.

Chaque build utilise un dossier neuf dans `<build_dir>/runs/` et révoque le reçu
précédent avant toute opération. Une sortie vide, un timing refusé, un `.bit`
incompatible ou des sources modifiées empêchent `arty-frame program` de
charger une ancienne configuration au titre de ce build. Avant programmation,
le reçu doit correspondre aux sources, à la configuration, au SHA256 du
bitstream, à son horloge et à son `BUILD_ID`. Cette vérification ne lance
aucun outil FPGA et peut précéder la fermeture de la liaison UART.
`jtag-program` est distinct : il
contrôle la cible et le fichier, sans attester un build récent des sources.
Le chargement est limité à la SRAM, perdue après coupure d'alimentation.

Sources amont : [openXC7/nextpnr](https://github.com/openXC7/nextpnr),
[distribution openXC7](https://github.com/cavearr/toolchain-openxc7-releases),
[Yosys](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/synth_xilinx.html),
[Project X-Ray](https://github.com/f4pga/prjxray),
[base Project X-Ray](https://github.com/f4pga/prjxray-db),
[openFPGALoader](https://github.com/trabucayre/openFPGALoader).
Voir également [le brochage](hardware.md) et [les vérifications](verification.md).
