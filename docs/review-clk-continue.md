# Revue de Claude et améliorations de la CLK continue

Cette revue porte sur les ajouts de Claude jusqu'au commit
[`7a7142f`](https://github.com/Citroz31/arty-frame-studio/commit/7a7142f0aae5b223e8db9511a69d79f6d8ccb42c),
intégrés avec la branche `main`. Le [guide utilisateur](guide-utilisateur-sipo-spi.md)
présente le parcours Windows et un exemple de SIPO à 10 MHz.

## Ce que les ajouts de Claude apportent

La révision 3 introduit la répétition illimitée des trames jusqu'à STOP.
La révision 4 ajoute **CLK libre** : l'horloge reste périodique pendant LATCH
et la pause, au lieu de s'interrompre après les bits. Le protocole UART annonce
ces capacités, les profils les enregistrent et le simulateur les reproduit.
Claude a aussi renforcé les tests RTL et le choix du placement FPGA : plusieurs
graines sont essayées pour obtenir une marge de timing.

Le moteur RTL est conservé. Les vérifications à chaque demi-tick, les essais
UART complets et un nouveau récepteur SIPO indépendant confirment son
fonctionnement pour les paramètres acceptés par l'application. Une CLK sans
interruption **et sans fin** exige d'activer les deux options :

| Répétition jusqu'à Arrêter | CLK libre | Comportement |
| --- | --- | --- |
| Non | Non | Nombre fixé de trames, CLK uniquement pendant les bits. |
| Oui | Non | Trames répétées sans fin, CLK basse pendant LATCH et pause. |
| Non | Oui | CLK périodique pendant la séquence, puis arrêt automatique. |
| Oui | Oui | CLK périodique sans fin, jusqu'à STOP, reset ou coupure. |

En CLK libre, DATA vaut zéro pendant LATCH et la pause. Ces fronts décalent
donc aussi des zéros dans un récepteur toujours actif. Un SIPO avec un registre
de sortie séparé peut capturer le mot avant ces fronts supplémentaires.
La polarité et le front effectif de capture doivent être vérifiés dans sa
fiche technique. LATCH n'est pas un signal CS SPI.

## Corrections et améliorations de cette revue

- **Lancement explicite.** Saisir `0` dans Répétitions pouvait lancer une
  émission indéfinie malgré l'option désactivée. Le champ exige désormais
  1 à 65 535 ; l'émission indéfinie se choisit avec le commutateur.
- **Compatibilité visible.** L'envoi est désactivé lorsqu'une carte connectée
  n'annonce pas la répétition continue ou CLK libre demandée. L'avertissement
  indique de charger le firmware à jour ou de désactiver les options.
  Arrêter reste accessible, même avec des paramètres invalides.
- **Commandes plus claires.** Le bouton devient Envoyer la trame,
  Démarrer la répétition ou Démarrer CLK continue selon les paramètres.
  Les commutateurs sont disposés verticalement, avec une explication du
  comportement et des fronts supplémentaires.
- **Durées réalisées.** En CLK libre, l'interface affiche le pas d'une période
  de CLK et les durées effectivement obtenues. À 10 MHz, une demande de LATCH
  de 20 ns donne 100 ns. Une saisie invalide efface les anciennes indications.
- **Connexion Windows.** Sans sélection déjà valide, le port USB FTDI
  `0403:6010` est proposé avant les ports Bluetooth. Cela n'identifie pas
  le firmware : PING et INFO restent nécessaires.
- **Déconnexion honnête.** Fermer un port actif ou dont l'état est incertain
  affiche un état non vérifié et invite à reconnecter puis Arrêter. Fermer
  l'UART ne produit pas STOP et ne termine pas l'émission autonome du FPGA.
- **Exemples immédiats.** Deux boutons chargent les profils SIPO et CLK seule
  à 10 MHz. Ils préparent les paramètres sans envoyer de commande, même si
  une autre émission est déjà active. Quatre profils et un SVG accompagnent
  le guide.
- **Aperçu borné.** Une seule trame très longue en CLK libre pouvait dépasser
  le budget d'affichage. La limite est maintenant de 50 000 transitions,
  même dans ce cas. Les trames complètes et la dernière trame partielle sont
  comptées séparément ; aucun retour fictif au repos n'est ajouté à la coupure.
  SVG et VCD annoncent la fenêtre limitée ; CSV conserve une précision adaptée
  aux horloges de cœur personnalisées.
- **Arrêt en ligne de commande.** `--duration` demande STOP à l'échéance sans
  lancer une lecture STATUS supplémentaire. Une interruption, un SEND non
  confirmé ou une erreur de suivi provoque au plus une tentative de STOP.
  Un refus BUSY ne stoppe pas une émission déjà existante. Une réponse STOP
  perdue n'est jamais présentée comme un arrêt confirmé.
- **Provenance des builds.** Chaque tentative de placement doit fournir de
  nouveaux FASM, rapport de timing et netlist routé, selon le backend utilisé.
  Les anciens fichiers sont retirés avant la tentative ; un résultat incomplet
  est refusé. Le repli sur le meilleur placement valide conserve ensemble ses
  trois fichiers et sa graine dans le reçu.

## Vérifications et limites

Les **486 tests Python passent**, ainsi que Ruff, le contrôle du format et mypy.
Six bancs Icarus couvrent le moteur, les ODDR, le protocole, le SIPO,
le chemin UART complet et l'UART à 200 MHz / 115200 bauds. Le nouveau banc SIPO
vérifie quatre modes et quatorze captures, dont `0xA5`, les fronts supplémentaires
en CLK libre, le cas d'une capture sur une autre polarité et STOP.

L'interface est vérifiée avec Flet 0.28.3 et Chromium aux tailles 1220 × 930
et 760 × 680 : profils d'exemple, SEND/STOP en démo, CLK continue, actions
visibles, saisie invalide et export du journal.
Les résultats détaillés figurent dans [verification.md](verification.md).

Le firmware fourni est celui de Claude, révision 4, routé à **210,79 MHz**
pour un cœur de 200 MHz, graine 8. Son SHA256 et ses huit sources compilées
sont revérifiés. Les changements de cette revue ne modifient pas ces sources :
ce firmware reste cohérent avec le dépôt. Les tests Python archivés avec lui
documentent sa construction initiale ; les tests de cette revue sont exécutés
séparément et par GitHub Actions.

**Aucune Arty physique n'est raccordée à l'environnement de vérification.**
Les essais logiciels ne valident ni le chargement sur votre carte, ni les
niveaux, ni les marges de la liaison externe. Le manifeste conserve
`hardware_validated: false`. L'arrêt par UART dépend du PC et de la liaison ;
STOP peut interrompre un bit ou une impulsion. Le chronogramme est une
simulation idéale d'une fenêtre finie, même pour une émission indéfinie.
