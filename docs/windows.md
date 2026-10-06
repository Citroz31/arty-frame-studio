# Windows natif : préparer, charger et piloter le firmware

Conserver le pilote Digilent Adept Runtime déjà installé, notamment **2.30.4**.
L'application utilise FTDI D2XX pour le JTAG et le port COM pour l'UART, avec
votre compte Windows. Avec Windows x64 et Python 64 bits, la préparation,
la compilation locale, le chargement et le pilotage se font sans WSL,
Linux ou PowerShell administrateur. Un `.bit` déjà fourni n'exige aucun compilateur.

**Préparer le firmware depuis Pilotage** réutilise un firmware compatible
vérifié ou prépare une compilation sur ce PC. Cette action conserve la trame ;
elle ne charge pas la carte et n'envoie pas SEND. GitHub reste facultatif.

**Un IDCODE `0x13631093` confirme que le JTAG reconnaît l'Arty A7-100T.**
COM7 permet l'accès série, mais PING ne répondra que si le firmware de ce
projet a été chargé. Le début RX `1b 5b 32 4a` correspond à `ESC[2J` ; du texte
et des astérisques reçus peuvent venir d'une démonstration déjà présente sur
la carte. Ce ne sont pas des réponses au protocole Arty Frame Studio.

Le [firmware précompilé](../firmware/prebuilt/arty_frame.bit) est fourni
dans `firmware/prebuilt/`. Sa synthèse et son routage ont réussi avec une
**Fmax de 210,79 MHz pour une contrainte de 200 MHz**. Un chargement SRAM
réussi a été rapporté par l'utilisateur ; son retour récent contient encore
des délais PING/INFO. Le dialogue courant et les sorties restent à confirmer
par les essais du [diagnostic local](rapport-diagnostic-local.md).

## Charger le fichier sous Windows

1. [Télécharger le ZIP mis à jour](https://github.com/Citroz31/arty-frame-studio/archive/refs/heads/main.zip)
   et l'extraire dans un dossier local court, **hors OneDrive**, par exemple
   `C:\ArtyFrameStudio` : la synchronisation peut verrouiller `.venv` ou les
   exports, et Windows limite les chemins à 260 caractères. Le journal le
   signale au démarrage. Le ZIP contient
   `firmware/prebuilt/arty_frame.bit`. Installer Python **3.11 ou plus récent,
   64 bits, pour votre utilisateur**, puis double-cliquer sur **`start-windows.cmd`**.
   Après une mise à jour qui ajoute une dépendance, le lanceur la complète au
   démarrage suivant ; `start-windows.cmd --setup-only`, depuis un terminal
   ordinaire, actualise aussi les dépendances. La simulation fonctionne sans carte.
2. Brancher l'Arty sur **USB PROG/UART** et fermer les autres applications JTAG
   ou série, notamment Adept et les terminaux qui utilisent COM7.
3. Ouvrir **FPGA**, section **1 · Charger le firmware**, puis **Détecter le FPGA sous
   Windows**. La détection lit l'IDCODE ; elle ne programme pas la carte.
4. Utiliser le **nouveau** `firmware/prebuilt/arty_frame.bit` du ZIP, avec
   RX sur A9 et TX sur D10. Remplacer votre ancienne copie au brochage inversé.
   Dans **Firmware existant pour l'Arty A7-100T (.bit)**,
   indiquer le chemin
   complet de `firmware\prebuilt\arty_frame.bit` dans le dossier extrait.
   L'application le présélectionne si ce fichier est présent. Cliquer sur
   **Charger le .bit sous Windows**. Prévoir environ **30 à 60 secondes**
   à la cadence JTAG de 1 MHz, puis attendre **SRAM chargée** dans le journal.
5. Vérifier la **première LED monochrome**, indicateur de verrouillage PLL de
   ce firmware. Dans **Pilotage**, sélectionner **Carte · USB / UART**,
   actualiser les ports, choisir **COM7**, puis cliquer sur **Connecter**.
   La connexion teste PING puis lit l'identité du firmware (INFO).
6. Cliquer sur **Tester les LED** : les quatre LED vertes LD4 à LD7 font un
   chenillard, s'allument ensemble, s'éteignent, puis reviennent à l'état.
   Si elles défilent, le bon firmware tourne sur cette carte et la liaison
   UART fonctionne dans les deux sens. Sinon, recharger le `.bit` à jour.
7. Commencer ensuite à fréquence réduite. Les sorties
   sont **JB1/E15 : DATA**, **JB2/E16 : CLK**, **JB3/D15 : LATCH** ; relier la
   masse sur **JB5 ou JB11**. Ce sont des signaux **3,3 V**. Voir
   [le brochage et les limites physiques](hardware.md).

**Vitesse du chargement.** Le `.bit` de 3,8 Mo se charge à **6 MHz** par défaut (environ 6 à 10 s), la valeur par défaut d'openFPGALoader. Auparavant la liaison JTAG était fixée à 1 MHz (35 s à une minute). Le menu **Vitesse JTAG (chargement SRAM)**, dans FPGA → Options JTAG, propose 1, 2, 3, 5, 6, 10, 15 et 30 MHz ; en ligne de commande : `arty-frame jtag-program --bitstream … --tck-mhz 10`. Si le FPGA ne répond pas à la vitesse choisie, l'application revient à 1 MHz **avant tout effacement** et l'indique dans le journal. Après un chargement, le journal donne la durée et la vitesse. Au-delà de 10 MHz, un câble USB court branché directement sur le PC est conseillé.

La programmation charge uniquement la **SRAM volatile**, pas la flash.
**Recharger le `.bit` après chaque coupure d'alimentation.** Le programme
présent en flash, par exemple une démonstration d'origine, peut revenir au
redémarrage. Une modification du mot, du diviseur de CLK, de la durée du latch,
de la pause ou des répétitions est envoyée par UART ; elle ne modifie pas le `.bit`.

Avant le chargement natif, le firmware fourni est contrôlé : SHA256 du `.bit`,
hashes des sources RTL/XDC et modèle de simulation, rapport de timing du cœur.
Une modification des sources exige un nouveau `.bit` et ses rapports. Garder
le dossier `firmware/prebuilt/` complet. Le même contrôle est disponible dans
un terminal ordinaire, à la racine du projet :

```cmd
.venv\Scripts\arty-frame.exe firmware-check
```

DTR et RTS sont désactivés avant l'ouverture de COM7. Si PING ne répond
toujours pas avec le nouveau firmware, vérifier le cavalier JP2, le bouton
RESET, LED0 et les autres applications qui peuvent ouvrir le port série.

Le chargement Windows vérifie la cible, l'IDCODE embarqué et le statut DONE.
Ce backend est testé avec une interface FTDI simulée ; la détection et le
chargement SRAM ont été rapportés par l'utilisateur. Aucun essai matériel
n'a été réalisé dans l'environnement de cette revue.

## Compiler sur ce PC avec des outils Windows portables

Le firmware de référence suffit pour les sorties JB1/JB2/JB3 à 10 MHz :
avec son cœur à 200 MHz, le diviseur vaut 20. La préparation réutilise aussi
une compilation locale déjà vérifiée quand sa configuration matérielle est
identique. Une nouvelle compilation est nécessaire pour changer l'horloge
du cœur, les broches DATA/CLK/LATCH, le courant de sortie ou le slew.

1. Dans **FPGA**, cliquer sur **Installer les outils Windows locaux**. Prévoir
   Internet au premier usage, environ **447 Mo** de téléchargements et au moins
   **4 Go libres**. L'application installe des versions épinglées de
   **OSS CAD Suite 2026-03-24** et **openXC7 2026-09-30**, vérifiées par taille et SHA256.
2. Les outils sont extraits sous
   `%LOCALAPPDATA%\ArtyFrameStudio\fpga-tools`. `toolchain.json` est créé dans
   le projet ; les builds et leurs reçus vont sous
   `%LOCALAPPDATA%\ArtyFrameStudio\builds`, dans un sous-dossier propre au projet.
   Ces caches évitent les chemins OneDrive et réduisent la longueur des chemins.
3. Cliquer sur **Vérifier les outils**. Choisir ensuite les broches et l'horloge
   du cœur dans **Pilotage** puis **Préparer le firmware depuis Pilotage**, ou
   utiliser **FPGA → Personnaliser le firmware → Compiler sur ce PC**.
   Une préparation compatible avec le `.bit` fourni ne télécharge aucun outil.
4. Attendre le résultat de synthèse et routage. Le timing final doit atteindre
   l'horloge demandée ; le `.bit`, le manifeste et les rapports sont vérifiés.
   Le résultat est sélectionné pour **Charger le .bit sous Windows**.
5. Charger le résultat, reconnecter l'UART, vérifier PING puis INFO, et revenir
   dans Pilotage pour **Envoyer** la trame conservée. Aucun SEND ni chargement
   n'est effectué automatiquement par la préparation ou la compilation.

La chaîne utilise Yosys ABC9, nextpnr/openXC7 et Project X-Ray natifs. Elle
n'installe pas de pilote ni de PATH système et ne demande pas de jeton GitHub.
Les archives peuvent être réutilisées depuis le cache vérifié. Une installation
incomplète ou un hash incorrect interrompt le parcours avec un diagnostic.
La compilation Windows native est en cours de validation dans cette revue ;
les tests d'installation simulés et l'inspection des archives sont distincts
d'une compilation réellement exécutée sous Windows.

Depuis un terminal CMD ordinaire, à la racine du projet :

```cmd
.venv\Scripts\arty-frame.exe install-fpga-tools --project-root .
.venv\Scripts\arty-frame.exe doctor --toolchain toolchain.json
.venv\Scripts\arty-frame.exe firmware-config --core-mhz 150 --data JB1 --clock JB2 --latch JB3 --output fw.json
.venv\Scripts\arty-frame.exe build --toolchain toolchain.json --firmware-config fw.json
```

Pour une CLK exacte de 150 MHz, choisir un cœur à 150 MHz. Avec le cœur de
référence à 200 MHz, l'application retient 100 MHz pour une demande de 150 MHz
afin de ne pas dépasser la fréquence demandée.

### GitHub facultatif

**Compiler sur GitHub** reste disponible dans la personnalisation du firmware.
Cette option demande un jeton GitHub avec Actions en lecture/écriture et
télécharge le `.bit` vérifié dans `builds\`. Aucun outil FPGA local n'est
alors nécessaire. Voir [la chaîne et ses rapports](toolchain.md).

## Paramètres JTAG et UART

Les champs **DLL FTDI D2XX (facultatif)** et **Série JTAG A (facultatif)**
peuvent rester vides avec une seule carte. Si plusieurs interfaces sont
présentes, sélectionner la série du **canal A/JTAG**, et conserver le
**canal B/UART** pour le port COM choisi. Exemples anonymisés :

| Interface | Identifiant |
| --- | --- |
| FPGA Arty A7-100T | IDCODE `0x13631093`, révision incluse |
| FTDI canal A/JTAG | Série du canal A donnée par la détection JTAG |
| FTDI canal B/UART | Série distincte du canal B, port COM courant, VID `0403`, PID `6010` |

Le numéro COM et les séries varient selon la carte et le PC. La DLL D2XX et
Python doivent avoir la même architecture, par exemple tous deux 64 bits.
La liaison UART est fixée à **115200 bauds, 8N1, sans contrôle de flux**.
Conserver les pilotes existants : aucune substitution WinUSB/Zadig n'est
nécessaire pour ce parcours.

## Si la connexion échoue

| Résultat | Vérification suivante |
| --- | --- |
| COM7 absent | Vérifier USB PROG/UART, alimentation et numéro COM actuel. |
| Port occupé ou accès refusé | Fermer les autres terminaux et applications utilisant ce port. |
| IDCODE `FFFFFFFF` ou `00000000` | Mettre à jour l'application, vérifier la série du canal A, le câble et l'alimentation ; fermer les autres outils JTAG. |
| DLL D2XX introuvable | Renseigner sa localisation dans le pilote installé ; vérifier l'architecture de Python et de la DLL. |
| JTAG reconnaît le 100T, PING expire | Charger le firmware du projet par JTAG ; la détection seule ne le fait pas. |
| PING expire après chargement | Vérifier le fichier chargé, COM7, la LED PLL et que le bouton rouge RESET n'est pas maintenu ; enregistrer le journal. |
| Des octets sont reçus sans réponse compatible | Vérifier que le bon firmware a été chargé depuis la dernière coupure ; le texte d'une autre démo n'est pas une réponse PING. |
| Des paquets INFO à CRC valide arrivent pendant PING | Conserver opcode et séquence reçus/attendus dans le journal ; une réponse INFO ne confirme pas PING. Fermer les autres applications UART et tester une connexion isolée. |
| « Réponse(s) tardive(s) à une requête précédente » ou « Liaison UART : réponses de la carte en retard » | La carte répond, mais après le délai : la réponse d'une requête arrive pendant la suivante. L'application mesure ce retard et allonge l'attente (jusqu'à 5 s) sans jamais accepter une réponse non corrélée. Fermer Keysight Connection Expert et les terminaux série, vérifier le Latency Timer du port FTDI (Gestionnaire de périphériques → port COM → Paramètres avancés, 16 ms par défaut), changer de câble ou de port USB, puis lancer `arty-frame diagnose --port COM7 --timeout 3` : il affiche le temps de réponse de PING. |
| « Décalage constant de N requête(s) » (par exemple PING séquence 0 → INFO séquence 2) | La carte renvoie à chaque requête la réponse d'une requête plus ancienne, même d'une session précédente : sa file de réponses est désynchronisée, le port COM n'est pas en cause. Cliquer sur **Réinitialiser la carte puis connecter** (impulsion DTR, cavalier JP2) ou appuyer sur le bouton **RESET** rouge de l'Arty (pas PROG), puis reconnecter. En ligne de commande : `arty-frame diagnose --port COM7 --timeout 3 --reset-board`. Si le décalage revient, le signaler avec le journal : il indique un défaut du firmware à corriger. |
| « Chaque réponse tardive est arrivée dès l'envoi de la requête suivante » | La réponse précédente ne sort qu'à l'arrivée d'une nouvelle requête : recharger le firmware vérifié, exporter le journal et transmettre la sortie de `diagnose`. |
| « Paquet(s) d'une session précédente ignoré(s) à l'ouverture » | Des réponses arrivées pendant que le port était fermé sont restées dans le convertisseur USB ; elles sont jetées avant le premier PING. |
| PING répond | Le firmware dialogue avec l'application ; commencer l'essai des sorties à fréquence réduite. |
| « Chaîne FPGA locale non configurée : toolchain.json est absent » | Cliquer sur **Installer les outils Windows locaux** pour compiler sur ce PC ; le chargement du `.bit` fourni reste disponible sans chaîne. GitHub est facultatif. |

Les chronogrammes de l'application sont idéaux. La compilation à 200 MHz
ne remplace pas une mesure CLK/DATA/LATCH et des marges du récepteur.

## Ajouter l'oscilloscope sans modifier le parcours Windows

L'onglet **Oscilloscope** fonctionne aussi depuis `start-windows.cmd`, avec
le même compte Windows. **Simulation (démo)** permet de préparer les réglages
sans appareil ; ses signaux ne sont pas des mesures de l'Arty.

Pour un **DSOX1202A** réel, choisir **Keysight · réseau LAN** si l'appareil
possède sa prise RJ45 arrière : le LAN est donné standard dans la fiche
Keysight actuelle. Relier le PC et l'oscilloscope au même réseau, lire l'adresse
dans **Utility → I/O → Configure → LAN → LAN Settings**, saisir cette adresse
dans l'application et **Connecter**. Le port TCP est **5025**. Cette liaison
utilise l'application Python et ne demande pas de pilote VISA ni de terminal
administrateur.

La liaison **Keysight · USB / VISA** utilise le port **USB Device arrière**
et une bibliothèque VISA avec pilote USB déjà présents. PyVISA, installé par
le lanceur, est seulement l'interface Python. L'installation de
[Keysight IO Libraries Suite](https://www.keysight.com/us/content/lib/software-detail/computer-software/io-libraries-suite-downloads-2175637/keysight-io-libraries-suite-2025.html)
demande des droits administrateur : sans ces droits et sans VISA existante,
utiliser le LAN ou la simulation. Les pilotes Digilent/FTDI de l'Arty sont
indépendants et restent ceux du parcours décrit ci-dessus.

Commencer la mesure à **1 MHz**, puis 10 MHz avec les sondes adaptées, les
facteurs sonde/voie accordés et une masse courte. La bande passante du
DSOX1202A est **70 MHz de base**, 100 ou 200 MHz selon l'option ; une fréquence
CLK de 200 MHz n'y suffit pas pour valider la forme des fronts et les marges
du récepteur. Voir [le guide Oscilloscope et ses sources Keysight](oscilloscope.md).

## Diagnostics facultatifs depuis un terminal ordinaire

Depuis le dossier du projet, dans CMD :

```bat
start-windows.cmd --diagnose-jtag
start-windows.cmd --diagnose-com7
.venv\Scripts\arty-frame.exe jtag-devices
.venv\Scripts\arty-frame.exe jtag-program --bitstream firmware\prebuilt\arty_frame.bit
```

Le diagnostic COM7 envoie uniquement PING, aucune commande SEND ou STOP.
Les commandes `jtag-diagnose` et `jtag-program` acceptent les options
`--serial "SERIE_JTAG_A"` et `--ftdi-dll "C:\FTDI\ftd2xx.dll"` si nécessaire,
en remplaçant le premier exemple par la série réelle du canal A.
Aucun utilitaire `djtgcfg.exe` n'est requis.

La chaîne locale, l'option de compilation distante et les rapports sont décrits dans
[toolchain.md](toolchain.md). Le fichier précompilé est accompagné de
[firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json),
[build.log](../firmware/prebuilt/build.log) et
[timing.json](../firmware/prebuilt/timing.json).

## Guide pas à pas

Pour une première utilisation et un câblage concret, consulter le
[guide utilisateur avec exemple SIPO à 10 MHz](guide-utilisateur-sipo-spi.md).
Il distingue la répétition des trames de la CLK libre et explique comment
charger le firmware, connecter le port COM et arrêter une émission.
