# Carte et raccordement

La carte ciblée est l'**Arty A7-100T** équipée du **xc7a100tcsg324-1**,
révisions D/E selon le fichier de contraintes Digilent utilisé. Ne programmer
ni une Arty 35T ni une autre carte avec ce bitstream.

L'USB FT2232 permet deux fonctions indépendantes : openFPGALoader charge le
FPGA par JTAG, puis le port série USB transmet les paramètres et les trames à
115200 bauds, 8N1. Le moteur matériel émet chaque séquence à la fréquence
choisie. Le débit USB influe sur l'envoi des commandes, pas sur les fronts
des signaux CLK/DATA/LATCH.

## Brochage fourni

| Fonction | Signal RTL | Broche FPGA | Connexion carte |
| --- | --- | --- | --- |
| Oscillateur 100 MHz | `clk100` | E3 | Oscillateur intégré |
| Reset actif bas | `reset_n` | C2 | Bouton rouge RESET / `ck_rst` |
| USB UART vers FPGA | `uart_rx` | A9 | FT2232 `uart_txd_in` |
| FPGA vers USB UART | `uart_tx` | D10 | FT2232 `uart_rxd_out` |
| Données série | `data_out` | E15 | JB broche 1 |
| Horloge série | `frame_clk` | E16 | JB broche 2 |
| Latch enable | `latch_enable` | D15 | JB broche 3 |
| Niveau TR (TX/RX), statique | `tr_out` | C15 | JB broche 4 |
| Masse | — | — | JB broche 5 ou 11 |
| LED verrouillage horloge | `led[0]` | H5 | LD4, première LED verte |
| LED émission en cours | `led[1]` | J5 | LD5 |
| LED réservée | `led[2]` | T9 | LD6 |
| LED trame terminée | `led[3]` | T10 | LD7 |

**TR** est une sortie LVCMOS33 **statique** (8 mA, fronts lents) : 3,3 V ou 0 V
selon la dernière commande, 0 V à la mise sous tension et après un reset. Elle
commande l'état émission/réception du composant (le niveau qui signifie TX se
choisit dans l'application) ; elle n'a pas d'horloge et se pilote depuis
Pilotage (interrupteur « TR à 3,3 V »), `arty-frame tr` ou le [mode VNA](mode-vna.md).
Relier sa masse comme pour les autres signaux.

Ce brochage est celui du firmware de référence. Le **test LED** de
l'application remplace pendant 3 secondes cet affichage par un chenillard
sur LD4 à LD7 : si les LED défilent, le bon firmware tourne sur cette carte et
la liaison UART fonctionne dans les deux sens. Voir [le protocole](protocol.md).

## Firmware personnalisé : horloge et broches

L'onglet **FPGA → Firmware personnalisé** (ou `arty-frame firmware-config`)
choisit l'horloge du cœur et les broches DATA, CLK, LATCH et TR parmi les 32
broches de signal des Pmod JA, JB, JC et JD, avec le courant (4 à 16 mA) et la
vitesse des fronts. Un nouveau `.bit` est compilé, localement ou sur GitHub
Actions ([chaîne FPGA](toolchain.md)) ; la carte annonce ensuite son horloge
et son identifiant par INFO.

| Pmod | Broches 1-4 | Broches 7-10 | Particularité |
| --- | --- | --- | --- |
| JA | G13, B11, A11, D12 | D13, B18, A18, K16 | 200 Ω en série |
| JB | E15, E16, D15, C15 | J17, J18, K15, J15 | paires L11, L12, L23, L24 |
| JC | U12, V12, V10, V11 | U14, V14, T13, U13 | paires L20, L21, L22, L23 |
| JD | D4, D3, F4, F3 | E2, D2, H2, G2 | 200 Ω en série |

L'horloge du cœur vaut **100 MHz × M / (D × O)** : M de 2 à 64, D de 1 à 5,
O de 1 à 128, avec un comparateur de phase (100 MHz / D) de 19 à 450 MHz et
un VCO de 800 à 1600 MHz (vitesse -1). Cela donne **6191 horloges distinctes
de 6,25 à 300 MHz**, arrondies au hertz pour INFO. CLK vaut cette horloge
divisée par N (1 à 65 535) ; le pas des durées vaut un demi-cycle du cœur.
Deux domaines d'horloge séparent l'UART et les commandes (100 MHz de la carte,
fixes) du moteur de trame, seul cadencé par le cœur.

### Fréquence demandée, fréquence la plus proche

Dans **Pilotage**, la fréquence demandée est comparée à toutes les
combinaisons PLL × N. La ligne « Plus proche réalisable » donne la fréquence
obtenue, l'écart, le cœur et le réglage PLL. Si le firmware chargé y parvient,
seul N change ; sinon **Adopter cette horloge** choisit ce cœur pour le
prochain firmware, à préparer (compilation locale ou GitHub) puis à charger.
**Ne jamais dépasser la fréquence demandée** retient la plus proche par valeur
inférieure, pour un récepteur qui ne tolère aucun dépassement.

| Demande | Plus proche | Cœur et PLL | N |
| --- | --- | --- | --- |
| 150 MHz | 150 MHz exacts | 150 MHz, ×9 / 1 / 6 | 1 |
| 151 MHz | 151,428571 MHz (150 MHz sans dépasser) | 151,428571 MHz, ×53 / 5 / 7 | 1 |
| 122 MHz | 122 MHz exacts | 244 MHz, ×61 / 5 / 5 | 2 |
| 120 MHz | 120 MHz exacts | 240 MHz, ×12 / 1 / 5 | 2 |
| 10 MHz | 10 MHz exacts, firmware de référence | 200 MHz, ×10 / 1 / 5 | 20 |

À écart égal, le planificateur préfère le firmware chargé, puis le firmware
de référence (aucune compilation), puis le cœur le plus proche de 200 MHz :
un pas des durées fin sans approcher la limite. En ligne de commande :
`arty-frame clock-plan 151 --output fw.json`, puis
`arty-frame build --firmware-config fw.json` (ou `remote-build`). L'écart
entre deux fréquences voisines vaut en moyenne 0,5 MHz vers 150-200 MHz et
1,5 MHz vers 250-300 MHz.

### Fréquence maximale de cette carte

| Limite | Valeur | Origine |
| --- | --- | --- |
| Sortie du PLL | 800 MHz | AMD DS181, vitesse -1 |
| Réseau d'horloge BUFG | 464 MHz | AMD DS181, vitesse -1 |
| Moteur de trame, timing routé | **300 MHz**, limite absolue appliquée | mesures ci-dessous |
| Sortie LVCMOS33 sur un Pmod | non spécifiée pour un signal carré | câblage et charge |

**Limite absolue : cœur à 300 MHz, donc CLK de 300 MHz au plus (N = 1).**
Au-delà, l'application et la CLI refusent la demande. Le moteur de trame est
écrit pour cette fréquence : un pas par bit et un pas de fin de trame d'au
moins deux ticks, des niveaux donnés par le signe de décompteurs, chaque
multiplexeur de rechargement dans une seule LUT devant la chaîne de retenue.
Mesures nextpnr-xilinx (48 placements, six réglages PLL de 250 à 300 MHz,
graines 1 à 8) : Fmax routée de 221 à 344 MHz selon le placement, environ un
placement sur trois au-dessus de 300 MHz, deux sur trois au-dessus de 275 MHz.
La compilation essaie jusqu'à 16 graines. Le firmware de référence, cœur à
200 MHz, atteint 288,93 MHz routés dès la graine 1. Ces rapports couvrent les
chemins entre registres ; ni la sortie DDR ni la liaison externe ne sont
certifiées.

À 300 MHz, CLK change toutes les 1,67 ns. Une sortie LVCMOS33 de 3,3 V à
travers un connecteur Pmod et des fils ne restitue plus un signal carré
propre : la limite ci-dessus est logique. La fréquence utilisable se mesure à
l'oscilloscope sur le montage réel, en montant progressivement.

L'application signale les choix électriquement risqués sans les interdire :
JA/JD et leurs résistances série, DATA et CLK sur la même paire de JB/JC,
sorties réparties sur plusieurs connecteurs, fronts lents, 4 mA. Un brochage
conseillé pour les fronts rapides : **CLK sur JB1, DATA sur JB3, LATCH sur
JB7**, chaque signal sur sa propre paire, masses JB5 et JB11 câblées.

Les numéros JB1/2/3 ci-dessus sont les positions physiques du connecteur.
Dans le fichier XDC Digilent, ces signaux sont nommés `jb[0]`, `jb[1]`,
`jb[2]` : l'index logique commence à zéro. JB6 et JB12 sont des alimentations
3,3 V, pas des signaux de données. Vérifier la sérigraphie et l'orientation
du connecteur avant câblage.

JB/JC sont les Pmod prévus pour les liaisons rapides, avec des paires de
pistes ; JA/JD comportent des résistances de protection de 200 Ω, pénalisantes
pour les fronts rapides. Le profil fourni choisit JB. Les sorties sont
utilisées en LVCMOS33 simples, pas comme une interface différentielle LVDS.
La présence d'un Pmod rapide ne certifie pas une liaison à haute fréquence. Vérifier
la révision et les schémas de votre carte avant de reproduire ce raccordement.

Le brochage provient du [Master XDC officiel Arty A7-100](https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc).
Digilent nomme `uart_rxd_out` et `uart_txd_in` du point de vue du PC (DTE) :
`uart_txd_in` (A9) porte les données émises par le PC, c'est une **entrée** du
FPGA ; `uart_rxd_out` (D10) porte les données reçues par le PC, c'est une
**sortie** du FPGA. LiteX utilise le même brochage (`tx` = D10, `rx` = A9).
Les versions antérieures du XDC inversaient ces deux broches : un `.bit`
compilé avec elles ne peut pas répondre à PING et pilote A9 contre la sortie
TXD du FT2232.

Le cavalier **JP2** relie le signal DTR du FT2232 à `ck_rst`, le reset du
FPGA. L'application désactive DTR et RTS avant d'ouvrir le port série ;
un autre terminal série qui active DTR peut maintenir ou relancer le reset. Le bouton **Réinitialiser la carte puis connecter** utilise volontairement ce lien : une impulsion DTR remet à zéro la logique du FPGA (file de réponses, émission en cours) sans recharger le firmware. Sans JP2, le bouton RESET rouge produit le même effet.

## Signaux et fréquence

L'interface implémente une trame **série**, un bit à la fois, de 1 à 26 bits.
Avec **CLK en rafales**, CLK est au repos bas entre les trames. Le premier
bit est présent avant le premier front montant ; le périphérique distant doit lire
les données au front montant. Les bits suivants changent aux fronts
descendants. Après le dernier front descendant, le moteur attend un
demi-cycle de CLK puis active le latch pendant sa durée réglée. Il applique
ensuite la pause entre répétitions. Une pause de zéro permet de réenchaîner
selon le moteur, sans garantie de flux USB continu.

Le paramètre N règle la fréquence à `horloge du cœur / N` et donne une durée de
demi-cycle de `N / (2 × horloge du cœur)`. Avec le firmware de référence,
cela donne `200 MHz / N`, un demi-cycle de `2,5 ns × N` et des durées
latch/pause multiples de 2,5 ns. Avec un firmware personnalisé, l'application
utilise l'horloge annoncée par la carte. Elle affiche la fréquence obtenue ;
un diviseur entier ne permet pas toutes les valeurs réelles. Les répétitions vont de 1 à 65535, ou
sont **continues** (firmware révision 3) : la trame se répète alors sans fin
jusqu'à STOP, sans dépendre de l'UART ni du PC. STOP interrompt l'émission
dans tous les cas. Le latch peut être actif haut ou bas.

Avec **CLK libre** (firmware révision 4), CLK ne s'interrompt plus pendant
LATCH et la pause : c'est une horloge périodique de `horloge du cœur / N`,
du début à la fin de l'émission, ou jusqu'à STOP en émission continue. LATCH commence au
front descendant qui suit le dernier bit et dure un nombre entier de
périodes, comme la pause ; DATA et LATCH changent uniquement aux fronts
descendants lorsque les paramètres respectent l'alignement imposé par
l'application. LATCH dure `N + latch_ticks` ticks, avec
`latch_ticks = (2k − 1)N` ; la pause vaut `2mN` ticks. Il faut activer à la
fois **CLK libre** et **émission continue** pour une horloge sans fin :
CLK libre seule se termine après le nombre de trames demandé.
À la fin ou au STOP, CLK/DATA reviennent à zéro et le latch à son niveau
inactif selon la polarité choisie. STOP est une interruption immédiate,
pas une fin de transaction garantie : le mot, le LATCH ou la dernière
impulsion CLK peuvent être incomplets. Pendant un reset physique, les ODDR
sont remis à zéro : ne dépendre pas d'un latch actif bas restant inactif au reset.

### Registre SIPO et SPI

Pour un SIPO qui décale au front montant, DATA se raccorde à l'entrée série
et CLK à son entrée de décalage. Si le composant dispose d'un registre de
sortie indépendant, LATCH peut commander sa validation selon la polarité et
le front indiqués dans sa fiche technique. Par exemple, le 74HC595 possède
une entrée de décalage SHCP et une entrée de mémorisation STCP, toutes deux
déclenchées au front montant : choisir **LATCH actif haut** pour STCP.
Pour commencer, conserver **CLK libre désactivée** : seuls les bits utiles
sont décalés et le latch arrive ensuite.

En CLK libre, un SIPO sans validation de décalage décale des zéros pendant
LATCH et la pause, même si la longueur du mot correspond à sa capacité.
Un registre de sortie déclenché au **front montant** conserve la bonne
valeur si LATCH est actif haut : il capture au dernier front descendant de
CLK, avant les zéros. Avec LATCH actif bas, son front montant arrive à la
fin du LATCH et peut capturer une valeur déjà décalée. Un latch transparent
peut suivre les changements du registre pendant son niveau actif. Vérifier
ces deux comportements avant d'utiliser CLK libre sur un SIPO.

Cette émission suit le timing de type **SPI mode 0** pendant les bits :
CLK démarre basse, DATA est prête avant le front montant et change au front
descendant. Le moteur n'implémente pas tous les modes SPI. **LATCH intervient
après la transmission ; il n'est pas un CS actif avant et pendant les bits.**
Ne pas le raccorder à l'entrée CS d'un composant SPI sans vérifier le protocole.
Le programme ne génère pas encore de CS de transaction, ne lit pas MISO et
ne propose pas CPOL/CPHA configurables. Un SIPO comme le 74HC595 se pilote
avec DATA/CLK/STCP ; un composant SPI exigeant un CS temporel nécessite un
moyen supplémentaire pour le fournir.

Exemple à 10 MHz avec le cœur de référence à 200 MHz : `N = 20`, période
de CLK de 100 ns, données stables 50 ns avant le front montant idéal.
Avec un mot de 8 bits, le dernier front montant est à 750 ns et le dernier
front descendant à 800 ns, relativement au premier bit prêt. En CLK en
rafales, LATCH commence à 850 ns ; en CLK libre, il commence à 800 ns.
Ces instants sont des valeurs logiques idéales : les délais et le décalage
entre les broches doivent être vérifiés sur le montage réel.

Toutes les sorties utilisent LVCMOS33. Ne pas relier directement un
périphérique 5 V ou un récepteur incompatible avec 3,3 V. Relier les masses.
Pour une liaison rapide, le connecteur, la longueur des fils, la charge, les
résistances de la carte et l'impédance du récepteur sont déterminants. Une
fréquence logique de 200 à 300 MHz ne garantit pas un signal exploitable à
l'extrémité d'un fil Dupont. Commencer avec un diviseur élevé, puis observer
CLK, DATA et LATCH sur un oscilloscope adapté et valider setup/hold du
récepteur avant d'augmenter la fréquence. Choisir un autre connecteur exige
de modifier les trois contraintes PACKAGE_PIN puis de recompiler.

Les rapports de timing du flux libre ne certifient pas les endpoints DDR ni
la liaison externe. La simulation de l'application représente des fronts
idéaux ; elle ne simule ni les overshoots, ni le temps de montée, ni le
skew des broches, ni la métastabilité d'un composant externe. La validation
physique au-delà de 10 MHz et les mesures sur carte du moteur révision 6
n'ont pas été réalisées ici.

## Sources

- [Arty A7 : manuel Digilent](https://digilent.com/reference/programmable-logic/arty-a7/reference-manual)
- [Contraintes officielles Rev. D/E](https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc)
- [Schémas et ressources de la carte](https://digilent.com/reference/programmable-logic/arty-a7/start)
- [AMD 7 Series SelectIO, UG471](https://docs.amd.com/v/u/en-US/ug471_7Series_SelectIO)
- [AMD 7 Series Clocking, UG472](https://docs.amd.com/v/u/en-US/ug472_7Series_Clocking)

Installation et limites de la chaîne : [toolchain.md](toolchain.md).
