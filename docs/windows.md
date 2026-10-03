# Windows : charger le firmware puis communiquer sur COM7

Conserver le pilote Digilent Adept Runtime déjà installé, notamment **2.30.4**.
L'application utilise FTDI D2XX pour le JTAG et le port COM pour l'UART, avec
votre compte Windows. Aucun WSL, Linux ou PowerShell administrateur n'est
nécessaire pour charger un firmware précompilé et piloter la carte.

**Un IDCODE `0x13631093` confirme que le JTAG reconnaît l'Arty A7-100T.**
COM7 permet l'accès série, mais PING ne répondra que si le firmware de ce
projet a été chargé. Le début RX `1b 5b 32 4a` correspond à `ESC[2J` ; du texte
et des astérisques reçus peuvent venir d'une démonstration déjà présente sur
la carte. Ce ne sont pas des réponses au protocole Arty Frame Studio.

Le [firmware précompilé](../firmware/prebuilt/arty_frame.bit) est fourni
dans `firmware/prebuilt/`. Sa synthèse et son routage ont réussi avec une
**Fmax de 206,14 MHz pour une contrainte de 200 MHz**. Il reste à tester
son chargement, PING et les sorties sur la carte réelle.

## Charger le fichier sous Windows

1. [Télécharger le ZIP mis à jour](https://github.com/Citroz31/arty-frame-studio/archive/refs/heads/main.zip)
   et l'extraire dans un dossier accessible à votre compte. Le ZIP contient
   `firmware/prebuilt/arty_frame.bit`. Installer Python **3.11 ou plus récent pour
   votre utilisateur**, puis double-cliquer sur **`start-windows.cmd`**.
   Après une mise à jour, `start-windows.cmd --setup-only`, depuis un terminal
   ordinaire, actualise les dépendances. La simulation fonctionne sans carte.
2. Brancher l'Arty sur **USB PROG/UART** et fermer les autres applications JTAG
   ou série, notamment Adept et les terminaux qui utilisent COM7.
3. Ouvrir **FPGA**, section **JTAG Windows natif**, puis **Détecter le FPGA sous
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

La programmation charge uniquement la **SRAM volatile**, pas la flash.
**Recharger le `.bit` après chaque coupure d'alimentation.** Le programme
présent en flash, par exemple une démonstration d'origine, peut revenir au
redémarrage. Ne pas utiliser **Compiler localement** ou **Programmer la SRAM**
du flux d'outils externes pour ce parcours de chargement Windows natif.

Pour changer l'horloge du cœur ou les broches sans outil FPGA sur le PC :
onglet **FPGA → Firmware personnalisé**, choisir les réglages, renseigner un
jeton GitHub (Actions : lecture et écriture) puis **Compiler sur GitHub**.
Le `.bit` vérifié est téléchargé dans `builds\` et proposé au chargement.
Voir [la compilation personnalisée](toolchain.md#firmware-personnalisé-depuis-linterface).

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
Ce backend est testé avec une interface FTDI simulée ; la détection JTAG a été
confirmée par un retour utilisateur. Le chargement de ce firmware et les
sorties physiques restent à vérifier sur une carte réelle.

## Paramètres JTAG et UART

Les champs **DLL FTDI D2XX (facultatif)** et **Série JTAG A (facultatif)**
peuvent rester vides avec une seule carte. Si plusieurs interfaces sont
présentes, sélectionner la série du **canal A/JTAG**, et conserver le
**canal B/UART** pour COM7. Dans la configuration rapportée :

| Interface | Identifiant |
| --- | --- |
| FPGA Arty A7-100T | IDCODE `0x13631093`, révision incluse |
| FTDI canal A/JTAG | Série `210319BE770AA` |
| FTDI canal B/UART | Série `210319BE770AB`, COM7, VID `0403`, PID `6010` |

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
| PING répond | Le firmware dialogue avec l'application ; commencer l'essai des sorties à fréquence réduite. |

Les chronogrammes de l'application sont idéaux. La compilation à 200 MHz
ne remplace pas une mesure CLK/DATA/LATCH et des marges du récepteur.

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
`--serial "210319BE770AA"` et `--ftdi-dll "C:\FTDI\ftd2xx.dll"` si nécessaire.
Aucun utilitaire `djtgcfg.exe` n'est requis.

La compilation distante et les rapports sont décrits dans
[toolchain.md](toolchain.md). Le fichier précompilé est accompagné de
[firmware-manifest.json](../firmware/prebuilt/firmware-manifest.json),
[build.log](../firmware/prebuilt/build.log) et
[timing.json](../firmware/prebuilt/timing.json).
