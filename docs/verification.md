# Vérifications réalisées et limites

Le firmware précompilé, révision 5 du protocole (LED, INFO, émission
continue, CLK libre et broche TR), a passé la synthèse, le placement/routage
et le contrôle strict à 200 MHz : **Fmax après routage 211,82 MHz** avec la
graine de placement 1, soit environ 6 % de marge. Les valeurs exactes de
chaque publication sont dans [le manifeste](../firmware/prebuilt/firmware-manifest.json).
Le compte rendu utilisateur décrit un chargement réussi et DATA observée à
10 MHz ; les sorties à 200 MHz et la qualification complète restent à tester.

## Logiciel et simulation

Les **1 026 tests Python passent** (CI Linux, Windows natif et job de publication
du firmware) : paramètres et profils, chronogrammes, protocole UART, transport,
interface, CLI, protections du flux FPGA, mode mesure (balayage de mots,
oscilloscope, instrument SCPI), mode VNA (liste d'états, pilote PNA simulé,
fichiers Touchstone, détection) et commande TR. Ruff, formatage et mypy
vérifient le code Python.

Le mode VNA est vérifié contre un **VNA simulé** qui reproduit le jeu de
commandes PNA (canaux, mesures S, balayage unique avec `*OPC?`, groupes de
balayages pour le moyennage, lecture `SDATA`, file d'erreurs). Les commandes
n'ont **pas** été essayées sur un N5245B ou un P9374A réel : voir
[le mode VNA](mode-vna.md) pour la liste des commandes à comparer au guide de
programmation de l'appareil.

La [revue oscilloscope](review-oscilloscope.md) vérifie les commandes avec le
guide 1200 X-Series, renforce la synchronisation LAN/VISA et les acquisitions,
et ajoute des régressions de mesure et de simulation. L'installation figée
`uv sync --extra dev --frozen` réussit avec PyVISA. Le rendu Flet/Chromium à
1220 × 930 et 760 × 680 vérifie Run/Stop/reprise, Single, Auto scale, préréglage,
connexion repliable, curseurs et CSV de 1000 points par voie. Il ne constitue
pas une validation SCPI matérielle.

La [revue de la saisie binaire](review-saisie-binaire.md) ajoute les contrôles
du nombre de bits automatique, des conversions sans changement de longueur,
du copier-coller, des profils à la fréquence minimale et de STOP malgré une
saisie invalide. Les 224 chargements de profils couvrent les 32 horloges
proposées et les diviseurs limites ; les essais Flet/Chromium confirment la
saisie et l'envoi en démo sur quatre et huit bits.

Ils couvrent aussi le refus du `.bit` obsolète, la cohérence fichier/manifeste/
sources/timing, les fréquences juste sous les 65 534 seuils réalisables et
les réponses SEND/STOP perdues avec ou sans réponse STATUS. Un SEND incertain
n'est pas répété ; l'interface exige STOP confirmé ou reconnexion avant de
permettre une nouvelle émission.

Les **six bancs Icarus Verilog passent** : **1 991 761 vérifications du
moteur** (dont le cycle d'armement qui suit SEND), **44 cas ODDR**, **51 cas
protocole** exécutés deux fois, avec un cœur à 238 MHz puis à 12,5 MHz face
au domaine de contrôle à 200 MHz (dont les formats valides et invalides de
LED et de TR, les six pages INFO, l'émission continue et la CLK libre),
chemin UART complet jusqu'aux sorties DATA/CLK/LATCH et liaison UART à
**100 MHz / 115200 bauds**, l'horloge de contrôle. Le banc moteur compare au modèle, à chaque
demi-tick, l'émission continue (`repeat_count` = 0), dont 70 000 trames
au-delà des rebouclages 16 bits, et la CLK libre (flags bit 2 : 189 cas finis
et 31 émissions continues, N jusqu'à 65 535), puis STOP à une phase
quelconque. Le banc de carte vérifie aussi le test LED par l'UART (motif,
retour automatique à l'état, retour immédiat), deux pages INFO, une émission
continue lancée, observée (plus de 100 000 fronts CLK, busy) puis arrêtée
par STOP, une émission continue en CLK libre (2 000 fronts montants en
10 µs à 200 MHz, sans interruption) puis STOP, deux resets lorsque
l'horloge est arrêtée, puis la reprise de fonctionnement. Il vérifie enfin la broche TR par l'UART :
0 V au départ, 3,3 V puis 0 V puis 3,3 V sur commandes valides, niveau inchangé
après une commande invalide, et retour à 0 V après un reset.

Le banc **SIPO** modélise indépendamment le registre de décalage
et le registre de sortie. Il vérifie quatre modes et **14 captures à 10 MHz**,
dont le mot `0xA5` en CLK en rafales et en CLK libre, les décalages de zéros
supplémentaires, une capture après désactivation d'un LATCH actif bas et STOP.
Les nouveaux tests Python couvrent aussi la limite de 50 000 transitions
d'une première trame partielle, la compatibilité du firmware, les exemples
d'interface, les arrêts CLI après erreur et le refus des fichiers hérités
d'une graine de placement précédente.

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
La revue du 4 octobre vérifie les fenêtres 1220 × 930 et 760 × 680, les
actions toujours visibles, SEND/STOP en démo, les fréquences invalides,
le redimensionnement des traces, les options avancées et l’export du journal.
Voir [le compte rendu de revue](review-2026-10-04.md).
La [revue de CLK continue](review-clk-continue.md) vérifie en complément les
boutons d'exemple SIPO et CLK seule à 10 MHz, le lancement et l'arrêt de CLK
continue en démo, les actions visibles dans une petite fenêtre et la
possibilité d'arrêter malgré une fréquence invalide.
Des distributions source et wheel ont été construites pendant le développement.
Les modèles de simulation PLL/ODDR ne sont jamais inclus dans la synthèse.

## Moteur révision 6 et fréquence maximale

Le moteur de trame a été réécrit pour la haute fréquence ; le banc moteur
inchangé (à un cycle d'armement près) sert d'oracle. L'UART et les paquets
restent à 100 MHz dans leur propre domaine d'horloge ; `engine_link` relie
les deux domaines par synchroniseurs et poignée de main, et `frame_plan`
calcule les constantes de chaque SEND dans le domaine de contrôle. Les
étapes successives du moteur ont été mesurées par placement/routage réel
(nextpnr-xilinx himbaechel `c68c1358`, ABC9), à 250 MHz et au-delà :

| Version du moteur | Fmax routée observée | Chemin limitant |
| --- | --- | --- |
| Révision 5, un seul domaine | 188-203 MHz | moteur, 5 à 7 niveaux de LUT |
| Deux domaines, moteur révision 5 | 197 MHz | moteur |
| Étapes BIT/TAIL et comparateurs | 159-210 MHz | comparateur puis 2 LUT |
| Décompteurs à bit de signe, multiplexeur avant l'additionneur | 224-246 MHz | LUT de passage avant la retenue |
| Valeur de rechargement unique (S à 3 entrées) | 235-287 MHz | contrôle à fort fan-out |
| Copies de `run` et `step_end` (version publiée) | 221-344 MHz | routage seul |

Version publiée, 48 placements (graines 1 à 8) sur six réglages PLL :

| Exigence | PLL | Graines qui passent | Fmax min-max |
| --- | --- | --- | --- |
| 250 MHz | ×10 / 1 / 4 | 6 / 8 | 227-327 MHz |
| 275 MHz | ×11 / 1 / 4 | 7 / 8 | 253-344 MHz |
| 275 MHz | ×55 / 4 / 5 | 5 / 8 | 262-342 MHz |
| 280 MHz | ×14 / 1 / 5 | 6 / 8 | 248-312 MHz |
| 290 MHz | ×29 / 2 / 5 | 2 / 8 | 252-327 MHz |
| 300 MHz | ×12 / 1 / 4 | 2 / 8 | 221-319 MHz |

Sur ces 48 placements, 69 % atteignent 275 MHz et 31 % 300 MHz ; avec
l'exigence de 300 MHz, 2 graines sur 8 passent. La limite absolue est donc
**300 MHz** : le build essaie jusqu'à 16 graines et, à 2 chances sur 8 par
graine, les 16 n'échouent toutes qu'environ une fois sur cent (0,75¹⁶). Le
domaine de contrôle passe partout à 100 MHz (Fmax 145 à 188 MHz). Le FASM de
chaque essai redonne exactement le réglage PLL demandé (M, D, O), y compris
les diviseurs d'entrée D = 2 et D = 4. Ces chiffres sont ceux du modèle de
timing de nextpnr-xilinx, pas une validation Vivado ni une mesure sur carte.

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

Le build réel de la révision 5 a terminé avec un code de sortie **0** et le
mapping **ABC9**. Le balayage des graines s'arrête à la première qui dépasse
l'exigence avec la marge de 3 % : la graine **1** donne
**211,82 MHz**, **PASS at 200.00 MHz** sur `core_clock`, soit environ 6 % de
marge, sans essayer les autres. La révision 4 avait au contraire besoin de la
graine 8 (210,79 MHz ; les graines 1, 2, 3, 5 et 7 échouaient entre 182 et
198 MHz). Les chemins limitants sont ceux du moteur de trame, où le routage
représente 75 à 85 % du délai : le placement fait varier la Fmax de 182 à
211 MHz pour le même RTL, d'où le balayage avec marge (voir
[toolchain](toolchain.md)). La sortie TR est un registre statique qui n'ajoute
aucun chemin critique.

Un build personnalisé à **150 MHz** (révision 3, run 37207946892, artefact seul) valide
aussi la période XDC précise : nextpnr applique une contrainte de
**150,01 MHz** (≥ 150 MHz) et le routage atteint 214,45 MHz. Les trois
ODDR conservent R→SR après routage. La provenance et les hashes du reçu
correspondent aux sources qui ont produit le fichier.

Le parser du projet accepte le `.bit` de **3 825 995 octets**, dont
**3 825 788 octets** de configuration, et son IDCODE **`0x03631093`**. SHA256 :

```text
8225942623b4e49bc8593195d44ece3323c724bbb8a2b93e651b5244c99aac52
```

Voir [firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json),
[build.log](../firmware/prebuilt/build.log) et
[timing.json](../firmware/prebuilt/timing.json). Le contrôle impose 200 MHz
et refuse tout résultat final absent ou en échec.

Ce fichier a été publié par le job `publish` du
[run 37473638607](https://github.com/Citroz31/arty-frame-studio/actions/runs/37473638607),
à partir du commit `d0b3c13`, après exécution de toute la suite Python sur le
nouveau contenu ([python-tests.log](../firmware/prebuilt/python-tests.log)).
Les fichiers RTL/XDC/modèle de primitives correspondent exactement au
manifeste. Les broches UART sont RX=A9, TX=D10. Les fins de lignes LF des `.v`/`.xdc` sont imposées par
`.gitattributes` pour conserver les empreintes lors d'un checkout Windows.

## Retours matériels et essai à effectuer

Le compte rendu fourni le 5 octobre 2026 rapporte une programmation SRAM
réussie sous Windows, le dialogue UART, le test LED et l'envoi de **26 bits**
(`0x1E15470`) avec CLK réglée à **10 MHz**. DATA a été observée sur **JB1**.
Le backend Windows reconnaît l'Arty A7-100T par JTAG : IDCODE **`0x13631093`**.
Ces retours complètent le diagnostic initial de COM7 sans firmware chargé.

Ces retours concernent la révision 4. Les révisions 5 (broche TR) et **6**
(moteur haute fréquence, deux domaines d'horloge) n'ont pas encore été
chargées sur une carte : vérifier après chargement que le firmware annonce
« révision 6 », que le test LED défile, mesurer TR (JB4) au voltmètre ou à
l'oscilloscope avec l'interrupteur « TR à 3,3 V » ou
`arty-frame tr --port COM7 1` (3,3 V) et `0` (0 V), puis augmenter CLK par
paliers en observant JB1/JB2/JB3 : 10, 50 et 100 MHz avec le firmware de
référence, au-delà avec un firmware personnalisé.

Aucune Arty réelle n'est raccordée à l'environnement de développement.
Nous n'avons pas reproduit ces essais dans cet environnement. Le contrôle
complet DATA/CLK/LATCH, les marges du récepteur, les sorties au-delà de
10 MHz (jusqu'à 300 MHz) et le pilotage SCPI d'un DSOX1202A réel restent à
effectuer. Le manifeste conserve `hardware_validated: false` : ce retour à
10 MHz ne constitue pas une qualification de toute la conception.

Après chargement sous Windows, vérifier la LED PLL puis PING avant tout SEND.
Commencer à fréquence réduite, observer DATA/CLK/LATCH sur JB1/JB2/JB3 et
contrôler les marges du récepteur avant d'augmenter la fréquence. Les modèles
PLL/ODDR ne représentent pas les effets analogiques ni setup/hold ; le Fmax
nextpnr ne certifie pas toute la sortie DDR et la liaison externe aux
fréquences élevées.
La procédure de chargement figure dans [windows.md](windows.md).
