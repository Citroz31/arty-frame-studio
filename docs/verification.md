# Vérifications réalisées

Validation logicielle dans l’environnement de développement, sans carte branchée :

- 278 tests Python passent : paramètres et profils, chronogrammes et exports,
  CRC/paquets et resynchronisation, transport avec UART simulée, commandes démo,
  interface, CLI et chaîne de compilation avec exécutables de test.
  Le diagnostic série distingue l’ouverture du port du PING compatible,
  indique le nombre d'octets et un aperçu RX limité à 32 octets, et ne transmet
  aucune commande SEND ou STOP.
- Le backend Windows FTDI D2XX est testé avec une DLL simulée et un modèle
  indépendant TAP/MPSSE : cible vérifiée avant JPROGRAM, ordre des bits,
  continuité des blocs USB, contrôle DONE/STAT et fermeture après erreur.
  Le modèle Digilent simule également buffers et multiplexeurs : le profil
  incomplet renvoie TDO constant ; le profil Arty complet permet la lecture IDCODE.
  Ce modèle suit les réglages amont et ne valide pas le schéma physique de la carte.
  Le parser `.bit` contrôle les métadonnées, les limites du fichier et l'IDCODE
  déclaré sans interpréter les données FDRI comme des commandes.
- Le workflow comprend un travail Windows natif : lanceur CMD, tests Python,
  Ruff et mypy. Les fixtures de processus à shebang Unix y sont ignorées ;
  un test de processus Python natif vérifie les chemins et arguments littéraux.
  Aucun test matériel FTDI ni accès à une Arty réelle n'est effectué par ce workflow.
- Ruff, formatage et mypy passent sur les modules Python.
- Cinq bancs Icarus Verilog passent, avec 467 832 demi-ticks contrôlés pour le
  moteur, 24 cas protocole et vérification des sorties ODDR, de l’UART et de la
  chaîne UART jusqu’aux sorties de la carte modélisée.
- Interface Flet 0.28.3 rendue avec Chromium : démarrage en mode démo et
  chronogramme visible avec le Canvas natif de Flet, sans exception JavaScript.
  Les modèles de carte
  et les primitives de simulation ne sont pas utilisés pour construire le FPGA.
- Distribution source et wheel construites. La distribution source comprend
  le RTL, les contraintes, les tests, les exemples et la documentation.

La synthèse matérielle, le placement/routage avec la base xc7a100tcsg324-1,
la programmation JTAG réelle et les mesures sur carte ne sont pas certifiés par
ces vérifications. Les modèles PLL/ODDR de simulation sont des modèles de
comportement, sans caractéristiques analogiques ni vérification setup/hold.
Le contrôle Fmax nextpnr de l’application porte sur les chemins de registres
modélisés par cet outil, pas sur la fermeture temporelle complète de l’interface DDR.

Après installation de la chaîne : inspecter `build/build.log`, valider le montage
à fréquence réduite, observer les trois sorties et vérifier les marges temporelles
du récepteur avant de demander les fréquences les plus élevées.
