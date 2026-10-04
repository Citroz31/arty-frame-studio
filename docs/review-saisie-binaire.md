# Revue de la saisie binaire et du nombre de bits automatique

La revue intègre les commits de Claude
[`86f3856`](https://github.com/Citroz31/arty-frame-studio/commit/86f38568a4a533f2cfec2fec63e23589a94e5eea)
et [`769f1b8`](https://github.com/Citroz31/arty-frame-studio/commit/769f1b8b5518461d57b5e1a52664a7f0a2fa56d7).
Le démarrage en **binaire** et le **nombre de bits automatique** sont conservés.
Le [guide utilisateur](guide-utilisateur-sipo-spi.md) décrit la saisie et le
pilotage d'un SIPO à 10 MHz.

## Ce que la mise à jour de Claude améliore

La trame s'affiche maintenant en binaire dès le démarrage. Chaque chiffre
compte comme un bit, y compris les zéros initiaux : `00000101` représente
8 bits, `101` seulement 3. Le champ Nombre de bits suit la saisie, sans
modification manuelle en mode binaire. Les groupes peuvent être séparés par
des espaces ou `_`, et `0b` est accepté comme préfixe. Une saisie vide,
incorrecte ou dépassant 26 bits empêche l'envoi.

Hexadécimal et décimal restent disponibles avec une longueur manuelle. Les
conversions valides et le chargement des profils conservent la valeur et la
longueur, ainsi que la notation sélectionnée pour l'affichage.

## Corrections et améliorations de cette revue

| Problème constaté | Comportement corrigé |
| --- | --- |
| Une saisie binaire invalide comme `1021` devenait un nombre hexadécimal valide en changeant la notation. | Le changement est refusé, le texte et la notation sont conservés, et un message demande de corriger ou d'effacer la saisie. |
| Le compteur dépendait de l'événement de saisie plutôt que de tous les rafraîchissements. | Chaque rafraîchissement synchronise le compteur avec le texte binaire, même après une saisie vide ou trop longue. |
| Effacer la trame pouvait laisser une longueur de 0 en revenant à une notation manuelle. | Une trame vide peut changer de notation ; le dernier nombre de bits valide est restauré en hexadécimal/décimal. Aucun bit n'est ajouté à une trame vide. |
| Les copier-coller avec tabulations, retours ligne ou espaces insécables étaient rejetés. | Tous les espaces blancs sont ignorés comme séparateurs. Les chiffres restent limités aux caractères autorisés de la notation. |
| Le formateur pouvait produire un mot plus long que la longueur annoncée. | Le type entier, la longueur 1–26 et la capacité du mot sont vérifiés avant le formatage. |
| Une fréquence invalide laissait certaines indications du dernier réglage valide. | L'aperçu binaire, l'ordre d'émission, les durées et le chronogramme sont effacés ensemble. |
| L'affichage à neuf chiffres pouvait arrondir un profil valide sous la fréquence minimale. | L'affichage utilise la précision nécessaire pour préserver les fréquences des profils et des diviseurs. |

Un préfixe seul n'est considéré comme vide que dans sa notation : `0b` en
binaire et `0x` en hexadécimal. **`0B` en hexadécimal reste le nombre 11** et
se convertit normalement. Une saisie binaire de 27 zéros est rejetée, même
si sa valeur numérique est zéro : sa longueur dépasse celle d'une trame.

Pendant une émission active, la modification de la saisie prépare une autre
trame ; elle ne change pas la séquence déjà envoyée. **Arrêter reste accessible**
avec un brouillon vide, invalide ou trop long.

Le README, le guide et les captures de l'interface sont actualisés. Le
récapitulatif historique de Claude précise désormais que les capacités et
l'horloge demandées sont contrôlées : un ancien firmware compatible peut
encore accepter une séquence finie en rafales.

## Vérifications

- **605 tests Python réussis**, dont les conversions, les caractères invalides,
  les profils sauvegardés/chargés/envoyés dans les trois notations, les zéros
  initiaux et STOP pendant une saisie invalide.
- **224 chargements de profils vérifiés**, couvrant les 32 horloges de cœur
  proposées et sept diviseurs, notamment 65 533, 65 534 et 65 535.
- Ruff, formatage et mypy réussis.
- Interface Flet vérifiée avec Chromium en 1220 × 930 et 760 × 680 : compteur
  automatique, transmission en démo de `0001` sur quatre bits et de `0xA5`
  sur huit bits, refus des saisies invalides, profils d'exemple, CLK continue,
  STOP et export du journal.
- Firmware précompilé : SHA256, sources RTL/XDC et rapport de timing
  revérifiés ; le RTL et le fichier `.bit` restent identiques à la version précédente.

**Cette mise à jour ne nécessite pas de reprogrammer le FPGA** si le firmware
révision 4 du projet est déjà chargé. Les tests d'émission de l'interface sont
effectués en démo ; aucune nouvelle validation matérielle n'est revendiquée.
