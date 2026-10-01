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
| USB UART vers FPGA | `uart_rx` | D10 | FT2232 `uart_rxd_out` |
| FPGA vers USB UART | `uart_tx` | A9 | FT2232 `uart_txd_in` |
| Données série | `data_out` | E15 | JB broche 1 |
| Horloge série | `frame_clk` | E16 | JB broche 2 |
| Latch enable | `latch_enable` | D15 | JB broche 3 |
| Masse | — | — | JB broche 5 ou 11 |
| LED verrouillage horloge | `led[0]` | H5 | Première LED monochrome |
| LED émission en cours | `led[1]` | J5 | Deuxième LED monochrome |
| LED réservée | `led[2]` | T9 | Troisième LED monochrome |
| LED trame terminée | `led[3]` | T10 | Quatrième LED monochrome |

Les numéros JB1/2/3 ci-dessus sont les positions physiques du connecteur.
Dans le fichier XDC Digilent, ces signaux sont nommés `jb[0]`, `jb[1]`,
`jb[2]` : l'index logique commence à zéro. JB6 et JB12 sont des alimentations
3,3 V, pas des signaux de données. Vérifier la sérigraphie et l'orientation
du connecteur avant câblage.

JB/JC sont les Pmod prévus pour les liaisons rapides, avec des paires de
pistes ; JA/JD comportent des résistances de protection de 200 Ω, pénalisantes
pour les fronts rapides. Le profil fourni choisit JB. Les sorties sont
utilisées en LVCMOS33 simples, pas comme une interface différentielle LVDS.
La présence d'un Pmod rapide ne certifie pas une liaison à 200 MHz. Vérifier
la révision et les schémas de votre carte avant de reproduire ce raccordement.

Le brochage provient du [Master XDC officiel Arty A7-100](https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc).
Les signaux `uart_rxd_out` et `uart_txd_in` sont nommés du point de vue de
l'interface USB, ce qui explique leur direction dans le RTL.

## Signaux et fréquence

L'interface implémente une trame **série**, un bit à la fois, de 1 à 26 bits.
CLK est normalement au repos bas et actif en rafales. Le premier bit est
présent avant le premier front montant ; le périphérique distant doit lire
les données au front montant. Les bits suivants changent aux fronts
descendants. Après le dernier front descendant, le moteur attend un
demi-cycle de CLK puis active le latch pendant sa durée réglée. Il applique
ensuite la pause entre répétitions. Une pause de zéro permet de réenchaîner
selon le moteur, sans garantie de flux USB continu.

Le paramètre N règle la fréquence à `200 MHz / N` et donne une durée de
demi-cycle de `2,5 ns × N`. Les durées latch/pause sont des multiples de
2,5 ns. L'application affiche la fréquence obtenue ; un diviseur entier ne
permet pas toutes les valeurs réelles. Les répétitions sont finies, de 1 à
65535, et STOP interrompt l'émission. Le latch peut être actif haut ou bas.
À la fin ou au STOP, CLK/DATA reviennent à zéro et le latch à son niveau
inactif selon la polarité choisie. Pendant un reset physique, les ODDR sont
remis à zéro : ne dépendre pas d'un latch actif bas restant inactif au reset.

Toutes les sorties utilisent LVCMOS33. Ne pas relier directement un
périphérique 5 V ou un récepteur incompatible avec 3,3 V. Relier les masses.
Pour une liaison rapide, le connecteur, la longueur des fils, la charge, les
résistances de la carte et l'impédance du récepteur sont déterminants. La
fréquence logique de 200 MHz ne garantit pas un signal exploitable à
l'extrémité d'un fil Dupont. Commencer avec un diviseur élevé, puis observer
CLK, DATA et LATCH sur un oscilloscope adapté et valider setup/hold du
récepteur avant d'augmenter la fréquence. Choisir un autre connecteur exige
de modifier les trois contraintes PACKAGE_PIN puis de recompiler.

Les rapports de timing du flux libre ne certifient pas les endpoints DDR ni
la liaison externe. La simulation de l'application représente des fronts
idéaux ; elle ne simule ni les overshoots, ni le temps de montée, ni le
skew des broches, ni la métastabilité d'un composant externe. La validation
physique à 200 MHz et les mesures sur carte n'ont pas été réalisées ici.

## Sources

- [Arty A7 : manuel Digilent](https://digilent.com/reference/programmable-logic/arty-a7/reference-manual)
- [Contraintes officielles Rev. D/E](https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc)
- [Schémas et ressources de la carte](https://digilent.com/reference/programmable-logic/arty-a7/start)
- [AMD 7 Series SelectIO, UG471](https://docs.amd.com/v/u/en-US/ug471_7Series_SelectIO)
- [AMD 7 Series Clocking, UG472](https://docs.amd.com/v/u/en-US/ug472_7Series_Clocking)

Installation et limites de la chaîne : [toolchain.md](toolchain.md).
