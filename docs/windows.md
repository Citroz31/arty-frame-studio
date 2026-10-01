# Windows : COM7 apparaît mais PING ne répond pas

Si seul le pilote Windows est installé et qu'aucun bitstream du projet n'a été
chargé, la connexion ne peut pas recevoir la réponse attendue au PING.
COM7 expose le pont USB/UART, mais le FPGA doit exécuter **notre firmware**
pour répondre au protocole binaire.
Un programme de démonstration présent sur la carte ne suffit pas.

**Ce dépôt ne fournit pas de `.bit` précompilé et validé sur carte.** Il faut
construire le firmware, puis le charger par JTAG avant de communiquer sur COM7.
Installer un pilote ne programme pas le FPGA.

## 1. Mettre à jour l'application Windows

Dans PowerShell, depuis le dossier du dépôt :

```powershell
git pull
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\arty-frame.exe ports
```

Si l'environnement `.venv` existe déjà, les deux dernières commandes suffisent
après `git pull`. Le port COM7 doit figurer dans la liste. Garder le pilote
FTDI VCP qui fait apparaître ce port.

## 2. Installer WSL2 pour construire le firmware

La chaîne libre décrite dans ce projet s'exécute sous Linux. Si WSL n'est pas
installé, ouvrir **PowerShell en administrateur**, puis :

```powershell
wsl --install -d Ubuntu
```

Redémarrer si Windows le demande, ouvrir Ubuntu et créer le compte Linux.
Dans Ubuntu, cloner le dépôt dans un dossier Linux, par exemple sous `~/`,
puis suivre [l'installation de la chaîne FPGA](toolchain.md) et les commandes
`arty-frame doctor` et `arty-frame build`. Installer les dépendances Python
du projet dans un environnement Linux distinct de la `.venv` Windows.

Cette préparation comprend Yosys, nextpnr-xilinx, Project X-Ray et la base 100T.
Elle demande davantage que l'installation du pilote : le `.bit` n'existe
qu'après une compilation et un contrôle de timing réussis. Les limites
de timing DDR et l'absence de validation physique sont précisées dans ce guide.

L'interface Flet lancée sous Windows n'appelle pas automatiquement les outils
installés dans Ubuntu. Pour cette étape, exécuter la CLI **dans Ubuntu**.
Construire le fichier ne demande aucun accès USB à la carte.

## 3. Charger le `.bit` par JTAG sous Windows

Après une compilation réussie, copier `build/arty_frame.bit` depuis Ubuntu
vers un dossier Windows, par exemple `C:\Arty\arty_frame.bit`. Les fichiers
Linux sont accessibles dans l'Explorateur via `\\wsl.localhost\Ubuntu\`.
Installer une version Windows d'openFPGALoader en suivant sa
[documentation officielle](https://trabucayre.github.io/openFPGALoader/guide/install.html),
puis, avec l'exécutable disponible dans le PATH :

```powershell
openFPGALoader -b arty_a7_100t C:\Arty\arty_frame.bit
```

Cette commande charge la **SRAM**, sans écrire la flash. La configuration
disparaît quand la carte est hors tension : la recharger après chaque coupure.
Avec ce firmware, la première LED monochrome `led[0]` indique le verrouillage
de la PLL ; elle doit s'allumer lorsque l'horloge interne est établie.

Le FT2232 présente deux fonctions : **JTAG** pour programmer et **UART/VCP**
pour COM7. La présence de COM7 ne confirme pas l'accès JTAG. Les exigences
de pilote JTAG dépendent du binaire openFPGALoader utilisé ; suivre son guide
et identifier l'interface concernée avant toute modification.
**Ne jamais remplacer le pilote de l'interface UART B/VCP par WinUSB avec
Zadig : cela peut faire disparaître COM7.** Aucune procédure de changement
de pilote n'est nécessaire pour expliquer le timeout constaté ici.

Cette programmation Windows est une étape manuelle distincte du build Linux.
La commande `arty-frame program` protège les builds au moyen de reçus et de
hashes propres à leur configuration ; copier seulement un `.bit` depuis Linux
ne transporte pas cette validation vers une autre configuration Windows.

WSL2 n'obtient pas automatiquement COM7 ni l'interface JTAG USB. Programmer
depuis WSL demanderait une configuration USB supplémentaire ; ce parcours
utilise openFPGALoader **natif Windows** pour éviter cette étape.

## 4. Vérifier COM7, puis ouvrir Flet

Fermer les terminaux série et l'application Flet avant le diagnostic : un seul
logiciel peut utiliser COM7 à la fois. Dans PowerShell, depuis le dépôt :

```powershell
.\.venv\Scripts\arty-frame.exe diagnose --port COM7 --timeout 2
```

Cette commande teste uniquement PING ; elle n'envoie aucune trame GPIO SEND.
La liaison utilise **115200 bauds, 8 bits, aucune parité, 1 bit d'arrêt**, sans
contrôle de flux. Un terminal texte ne parle pas le protocole binaire du projet.

Si PING réussit, lancer :

```powershell
.\.venv\Scripts\arty-frame-studio.exe
```

Choisir **« Carte · USB / UART »**, actualiser les ports, sélectionner **COM7**,
puis **Connecter**. Le mode « Démo locale » ne communique pas avec la carte.

| Résultat | Vérification suivante |
| --- | --- |
| COM7 absent | Câble USB, carte alimentée, pilote FTDI VCP et numéro COM actuel. |
| Accès au port refusé | Fermer l'autre logiciel qui utilise COM7. |
| PING expire | Vérifier que notre `.bit` a été chargé depuis la dernière coupure, que `led[0]` est allumée et que COM7 correspond bien à l'UART de cette carte. |
| PING réussit | Le firmware répond ; vous pouvez ensuite régler et envoyer une trame. |

Le raccordement des sorties est décrit dans [hardware.md](hardware.md) :
DATA sur JB1, CLK sur JB2 et LATCH sur JB3, en logique 3,3 V.
