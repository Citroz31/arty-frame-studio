# Vérifications réalisées et limites

Le firmware précompilé a passé la synthèse, le placement/routage et le
contrôle strict à 200 MHz : **Fmax après routage 210,44 MHz**. Le chargement
de ce firmware et ses sorties physiques restent à tester sur la carte.

## Logiciel et simulation

Les **296 tests Python passent** : paramètres et profils, chronogrammes,
protocole UART, transport, interface, CLI et protections du flux FPGA.
Ruff, formatage et mypy vérifient le code Python.

Les **cinq bancs Icarus Verilog passent** : **1 124 018 vérifications du
moteur**, **42 cas ODDR**, **24 cas protocole**, chemin UART complet jusqu'aux
sorties DATA/CLK/LATCH et liaison UART à **200 MHz / 115200 bauds**. Le banc
de carte vérifie aussi deux resets lorsque l'horloge est arrêtée, puis la
reprise de fonctionnement.

Le backend Windows FTDI D2XX possède des tests avec DLL simulée et modèle
TAP/MPSSE indépendant : contrôle de cible avant JPROGRAM, ordre des bits,
continuité des transferts, DONE/STAT et fermeture après erreur. Le modèle
Digilent vérifie le profil GPIO Arty complet. Le parser `.bit` contrôle les
métadonnées et l'IDCODE sans interpréter les données FDRI comme des commandes.
Ces modèles ne remplacent pas un essai physique.

Le diagnostic série envoie uniquement PING, jamais SEND ou STOP ; il indique
les identifiants USB, le nombre d'octets reçus et un aperçu limité à 32 octets.
Le workflow de tests comprend Windows natif, le lanceur CMD, Python, Ruff
et mypy. La présence d'un workflow ne signifie pas qu'une carte physique
est connectée aux tests.

L'interface Flet 0.28.3 a été rendue avec Chromium : démarrage en démo et
chronogramme affiché avec le Canvas natif, sans exception JavaScript.
Des distributions source et wheel ont été construites pendant le développement.
Les modèles de simulation PLL/ODDR ne sont jamais inclus dans la synthèse.

## Compilation réelle

Les archives épinglées OSS CAD Suite **2026-03-24** et openXC7 **2026-09-30**
ont été extraites et exécutées dans l'environnement de développement.
Le bootstrap a vérifié leurs SHA256 et la présence de la chipdb 100T et de
la base Project X-Ray. Yosys est en version **0.63+173**, commit `66306a8ca` ;
nextpnr himbaechel est au commit `c68c1358`.

La synthèse réelle a révélé que `COMPENSATION` appartient à `PLLE2_ADV`,
pas à `PLLE2_BASE` : le RTL et le modèle de simulation ont été corrigés.
Le flux vérifie aussi le maintien du reset asynchrone R des trois ODDR dans
le netlist routé après normalisation des seules entrées S inactives.
Voir [les adaptations et le contrôle de timing](toolchain.md).

Le build réel a terminé avec un code de sortie **0**, le mapping **ABC9** et
la graine nextpnr par défaut **1**. Cible : `xc7a100tcsg324-1` ; Fmax finale :
**210,43771362304688 MHz**, **PASS at 200.00 MHz** sur `core_clock`. Les trois
ODDR conservent R→SR après routage. La provenance et les hashes du reçu
correspondent aux sources qui ont produit le fichier.

Le parser du projet accepte le `.bit` de **3 825 970 octets**, dont
**3 825 788 octets** de configuration, et son IDCODE **`0x03631093`**. SHA256 :

```text
5849a6ffaf0cf05d3823e250ac7f6091d8c219c15194b61eabd411823e2e6b4e
```

Voir [firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json),
[build.log](../firmware/prebuilt/build.log) et
[timing.json](../firmware/prebuilt/timing.json). Le contrôle impose 200 MHz
et refuse tout résultat final absent ou en échec.

## Retours matériels et essai à effectuer

Un utilisateur a confirmé que le backend Windows reconnaît son Arty A7-100T
par JTAG : IDCODE **`0x13631093`**. Son port **COM7**, canal FTDI B, reçoit des
octets mais aucun PING compatible avant chargement du firmware du projet.
Cette observation confirme l'accès JTAG et série ; elle ne valide pas le
chargement de notre firmware ni ses sorties GPIO.

Aucune Arty réelle n'est raccordée à l'environnement de développement.
La programmation du `.bit`, la réponse PING après chargement et les mesures
physiques restent donc à confirmer sur la carte. Le manifeste du firmware
indique `hardware_validated: false`.

Après chargement sous Windows, vérifier la LED PLL puis PING avant tout SEND.
Commencer à fréquence réduite, observer DATA/CLK/LATCH sur JB1/JB2/JB3 et
contrôler les marges du récepteur avant d'augmenter la fréquence. Les modèles
PLL/ODDR ne représentent pas les effets analogiques ni setup/hold ; le Fmax
nextpnr ne certifie pas toute la sortie DDR et la liaison externe à 200 MHz.
La procédure de chargement figure dans [windows.md](windows.md).
