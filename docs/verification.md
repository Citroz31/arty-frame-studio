# Vérifications réalisées et limites

Le firmware précompilé, révision 2 du protocole (LED et INFO), a passé la
synthèse, le placement/routage et le contrôle strict à 200 MHz : **Fmax après
routage 206,14 MHz** avec la graine de placement 4. Les valeurs exactes de
chaque publication sont dans [le manifeste](../firmware/prebuilt/firmware-manifest.json).
Le chargement de ce firmware et ses sorties physiques restent à tester sur la
carte.

## Logiciel et simulation

Les **385 tests Python passent** : paramètres et profils, chronogrammes,
protocole UART, transport, interface, CLI et protections du flux FPGA.
Ruff, formatage et mypy vérifient le code Python.

Ils couvrent aussi le refus du `.bit` obsolète, la cohérence fichier/manifeste/
sources/timing, les fréquences juste sous les 65 534 seuils réalisables et
les réponses SEND/STOP perdues avec ou sans réponse STATUS. Un SEND incertain
n'est pas répété ; l'interface exige STOP confirmé ou reconnexion avant de
permettre une nouvelle émission.

Les **cinq bancs Icarus Verilog passent** : **1 124 018 vérifications du
moteur**, **42 cas ODDR**, **38 cas protocole** (dont les formats valides et
invalides de LED et les six pages INFO), chemin UART complet jusqu'aux
sorties DATA/CLK/LATCH et liaison UART à **200 MHz / 115200 bauds**. Le banc
de carte vérifie aussi le test LED par l'UART (motif, retour automatique à
l'état, retour immédiat), une page INFO, deux resets lorsque l'horloge est
arrêtée, puis la reprise de fonctionnement.

Ils couvrent aussi la configuration de firmware personnalisé (table PLL,
broches Pmod, XDC de référence identique au dépôt, avertissements), le build
personnalisé avec outils simulés, le balayage des graines nextpnr, la
compilation GitHub contre une API simulée (téléchargement, ZIP hostile,
manifeste et configuration discordants) et l'adaptation de l'interface à
l'horloge annoncée par la carte.

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
ont été extraites et exécutées dans l'environnement de développement et
sur GitHub Actions pour le firmware corrigé.
Le bootstrap a vérifié leurs SHA256 et la présence de la chipdb 100T et de
la base Project X-Ray. Yosys est en version **0.63+173**, commit `66306a8ca` ;
nextpnr himbaechel est au commit `c68c1358`.

La synthèse réelle a révélé que `COMPENSATION` appartient à `PLLE2_ADV`,
pas à `PLLE2_BASE` : le RTL et le modèle de simulation ont été corrigés.
Le flux vérifie aussi le maintien du reset asynchrone R des trois ODDR dans
le netlist routé après normalisation des seules entrées S inactives.
Voir [les adaptations et le contrôle de timing](toolchain.md).

Le build réel a terminé avec un code de sortie **0** et le mapping **ABC9**.
Les graines 1 à 3 ont échoué après routage (188,7, 199,6 et 165,5 MHz) ; la
graine **4** donne **206,14306640625 MHz**, **PASS at 200.00 MHz** sur
`core_clock`. La marge est donc d'environ 3 %, limitée par des chemins du
moteur de trame où le routage représente 75 à 85 % du délai. Les trois
ODDR conservent R→SR après routage. La provenance et les hashes du reçu
correspondent aux sources qui ont produit le fichier.

Le parser du projet accepte le `.bit` de **3 825 995 octets**, dont
**3 825 788 octets** de configuration, et son IDCODE **`0x03631093`**. SHA256 :

```text
859ad3674bcd2505c6f510436a5f123c3004f208be9fde969338a82e8f47effb
```

Voir [firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json),
[build.log](../firmware/prebuilt/build.log) et
[timing.json](../firmware/prebuilt/timing.json). Le contrôle impose 200 MHz
et refuse tout résultat final absent ou en échec.

Ce fichier a été publié par le job `publish` du
[run 37142507552](https://github.com/Citroz31/arty-frame-studio/actions/runs/37142507552),
à partir du commit `d588999`, après exécution de toute la suite Python sur le
nouveau contenu ([python-tests.log](../firmware/prebuilt/python-tests.log)).
Les fichiers RTL/XDC/modèle de primitives correspondent exactement au
manifeste. Les broches UART sont RX=A9, TX=D10. Les fins de lignes LF des `.v`/`.xdc` sont imposées par
`.gitattributes` pour conserver les empreintes lors d'un checkout Windows.

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
