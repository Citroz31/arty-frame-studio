# Firmware Arty A7-100T précompilé

Firmware de **référence** : cœur à 200 MHz, DATA/CLK/LATCH sur JB1/JB2/JB3,
UART **RX = A9, TX = D10**, 115200 bauds 8N1. **Révision 4 du firmware**
(format de paquets version 1) : commandes **LED** (test visuel sur LD4-LD7),
**INFO** (horloge du cœur, identifiant de build 0), émission continue jusqu'à
STOP et CLK libre pendant LATCH/pause. Les six entrées des ODDR sont enregistrées ; leur
reset asynchrone R→SR est contrôlé dans le netlist routé.

Ce dossier est produit par le workflow **Build Arty A7-100T firmware**, lancé
avec `publish_prebuilt` : build, simulations RTL, timing du cœur à 200 MHz,
puis suite Python complète sur ce contenu avant le commit. Le
[manifeste](firmware-manifest.json) donne le commit des sources, l'exécution
GitHub, le SHA256 du `.bit`, l'IDCODE, les versions des outils, la Fmax après
routage et les hashes de toutes les sources `.v` et `.xdc`. **Aucun essai sur
carte physique n'a été réalisé ici** (`hardware_validated: false`).

Dans l'application Windows, onglet FPGA, utiliser **Charger le .bit sous
Windows**. Le fichier est présélectionné. Attendre 30 à 60 secondes, vérifier
LD4 (PLL verrouillé), connecter COM7, puis cliquer sur **Tester les LED**.
SRAM volatile : recharger après une coupure d'alimentation. Garder les pilotes
Adept existants ; aucun Linux, WSL ou droit administrateur n'est nécessaire.

Le chargement contrôle le `.bit`, son manifeste, les sources et le timing du
cœur ; `arty-frame firmware-check` et les tests CI font le même contrôle. Le
fichier initial aux broches UART inversées est refusé même après renommage.
Conserver ensemble le `.bit`, le manifeste, `timing.json` et les rapports. Les
chemins internes du reçu et du journal sont ceux du runner GitHub. Le Fmax du
cœur ne certifie pas les sorties DDR ni la liaison Pmod. Pour une autre
horloge ou d'autres broches, voir [la compilation personnalisée](../../docs/toolchain.md).

**Préparer le firmware depuis Pilotage** réutilise ce `.bit` pour les réglages
matériels de référence : le mot, le diviseur de CLK, le latch et les répétitions
restent des paramètres UART. Pour une autre horloge du cœur, d'autres broches
ou paramètres électriques, utiliser **Installer les outils Windows locaux**
puis **Compiler sur ce PC** ; les outils et builds sont conservés dans le
cache utilisateur `%LOCALAPPDATA%\ArtyFrameStudio`, hors OneDrive. Le parcours
Windows est natif et ne demande pas Linux, WSL ou droits administrateur.
La compilation GitHub reste facultative. Aucun chargement ni SEND n'est lancé
automatiquement par la préparation. Voir le [guide Windows](../../docs/windows.md).
