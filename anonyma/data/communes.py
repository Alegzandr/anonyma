"""Real French communes with one of their actual postcodes, for realistic addresses.

Hand-curated facts (postcode, commune name), chosen for variety of size and region:
prefectures and sub-prefectures, suburbs, small towns. Not copied from any database.
A substitute address must read as a real place: an invented commune or a postcode
that does not exist gives the anonymisation away.
"""

from __future__ import annotations

from functools import lru_cache

_COMMUNES = """
01000 Bourg-en-Bresse|01100 Oyonnax|01300 Belley|02000 Laon|02100 Saint-Quentin
02200 Soissons|02300 Chauny|03000 Moulins|03100 Montluçon|03200 Vichy
03500 Saint-Pourçain-sur-Sioule|04000 Digne-les-Bains|04100 Manosque|04300 Forcalquier
05000 Gap|05100 Briançon|05200 Embrun|06000 Nice|06130 Grasse|06140 Vence|06400 Cannes
06500 Menton|06600 Antibes|06800 Cagnes-sur-Mer|07000 Privas|07150 Vallon-Pont-d'Arc
07200 Aubenas|08000 Charleville-Mézières|08200 Sedan|09000 Foix|09100 Pamiers
09200 Saint-Girons|10000 Troyes|10200 Bar-sur-Aube|11000 Carcassonne|11100 Narbonne
11300 Limoux|12000 Rodez|12100 Millau|12200 Villefranche-de-Rouergue|13001 Marseille
13100 Aix-en-Provence|13200 Arles|13210 Saint-Rémy-de-Provence|13300 Salon-de-Provence
13400 Aubagne|13500 Martigues|13600 La Ciotat|14000 Caen|14100 Lisieux|14400 Bayeux
14800 Deauville|15000 Aurillac|15100 Saint-Flour|16000 Angoulême|16100 Cognac
16200 Jarnac|17000 La Rochelle|17100 Saintes|17200 Royan|17300 Rochefort
17410 Saint-Martin-de-Ré|18000 Bourges|18100 Vierzon|18200 Saint-Amand-Montrond
19000 Tulle|19100 Brive-la-Gaillarde|19120 Beaulieu-sur-Dordogne|20000 Ajaccio
20137 Porto-Vecchio|20169 Bonifacio|20200 Bastia|20260 Calvi|21000 Dijon|21200 Beaune
21500 Montbard|22000 Saint-Brieuc|22100 Dinan|22300 Lannion|22500 Paimpol
22700 Perros-Guirec|23000 Guéret|23200 Aubusson|24000 Périgueux|24100 Bergerac
24200 Sarlat-la-Canéda|25000 Besançon|25200 Montbéliard|25290 Ornans|25300 Pontarlier
26000 Valence|26100 Romans-sur-Isère|26110 Nyons|26200 Montélimar|27000 Évreux
27200 Vernon|27500 Pont-Audemer|28000 Chartres|28100 Dreux|28400 Nogent-le-Rotrou
29000 Quimper|29120 Pont-l'Abbé|29200 Brest|29600 Morlaix|29900 Concarneau|30000 Nîmes
30100 Alès|30220 Aigues-Mortes|30700 Uzès|31000 Toulouse|31770 Colomiers
31800 Saint-Gaudens|32000 Auch|32100 Condom|33000 Bordeaux|33120 Arcachon|33250 Pauillac
33400 Talence|33500 Libourne|33600 Pessac|33700 Mérignac|34000 Montpellier|34200 Sète
34300 Agde|34400 Lunel|34500 Béziers|34700 Lodève|35000 Rennes|35170 Bruz
35300 Fougères|35400 Saint-Malo|35500 Vitré|35800 Dinard|36000 Châteauroux
36200 Argenton-sur-Creuse|37000 Tours|37400 Amboise|37500 Chinon|37600 Loches
38000 Grenoble|38200 Vienne|38300 Bourgoin-Jallieu|38400 Saint-Martin-d'Hères
39000 Lons-le-Saunier|39100 Dole|39200 Saint-Claude|40000 Mont-de-Marsan|40100 Dax
40200 Mimizan|41000 Blois|41100 Vendôme|41200 Romorantin-Lanthenay|42000 Saint-Étienne
42300 Roanne|42600 Montbrison|43000 Le Puy-en-Velay|43100 Brioude|44000 Nantes
44350 Guérande|44400 Rezé|44500 La Baule-Escoublac|44600 Saint-Nazaire|44800 Saint-Herblain
45000 Orléans|45200 Montargis|45500 Gien|46000 Cahors|46100 Figeac|46500 Rocamadour
47000 Agen|47200 Marmande|47300 Villeneuve-sur-Lot|48000 Mende|49000 Angers|49300 Cholet
49400 Saumur|50000 Saint-Lô|50100 Cherbourg-en-Cotentin|50170 Pontorson|50400 Granville
51000 Châlons-en-Champagne|51100 Reims|51200 Épernay|51300 Vitry-le-François
52000 Chaumont|52100 Saint-Dizier|53000 Laval|54000 Nancy|54400 Longwy
54700 Pont-à-Mousson|55000 Bar-le-Duc|55100 Verdun|55200 Commercy|56000 Vannes
56100 Lorient|56300 Pontivy|56340 Carnac|56400 Auray|57000 Metz|57100 Thionville
57200 Sarreguemines|57400 Sarrebourg|58000 Nevers|58200 Cosne-Cours-sur-Loire|59000 Lille
59100 Roubaix|59140 Dunkerque|59190 Hazebrouck|59200 Tourcoing|59300 Valenciennes
59400 Cambrai|59500 Douai|59650 Villeneuve-d'Ascq|59700 Marcq-en-Barœul|60000 Beauvais
60100 Creil|60200 Compiègne|60300 Senlis|61000 Alençon|61100 Flers|61300 L'Aigle
62000 Arras|62100 Calais|62200 Boulogne-sur-Mer|62300 Lens|62400 Béthune|62500 Saint-Omer
62600 Berck|63000 Clermont-Ferrand|63150 La Bourboule|63200 Riom|63300 Thiers|64000 Pau
64100 Bayonne|64200 Biarritz|64500 Saint-Jean-de-Luz|65000 Tarbes|65100 Lourdes
65200 Bagnères-de-Bigorre|66000 Perpignan|66190 Collioure|67000 Strasbourg|67140 Barr
67500 Haguenau|67600 Sélestat|68000 Colmar|68100 Mulhouse|69001 Lyon|69100 Villeurbanne
69300 Caluire-et-Cuire|69400 Villefranche-sur-Saône|69500 Bron|70000 Vesoul|70200 Lure
71000 Mâcon|71100 Chalon-sur-Saône|71200 Le Creusot|72000 Le Mans|72200 La Flèche
72300 Sablé-sur-Sarthe|73000 Chambéry|73100 Aix-les-Bains|73120 Courchevel
73200 Albertville|74000 Annecy|74100 Annemasse|74200 Thonon-les-Bains
74400 Chamonix-Mont-Blanc|75001 Paris|75015 Paris|76000 Rouen|76200 Dieppe|76400 Fécamp
76470 Le Tréport|76600 Le Havre|77000 Melun|77100 Meaux|77160 Provins|77300 Fontainebleau
77400 Lagny-sur-Marne|78000 Versailles|78100 Saint-Germain-en-Laye|78180 Montigny-le-Bretonneux
78200 Mantes-la-Jolie|78300 Poissy|79000 Niort|79100 Thouars|79200 Parthenay|80000 Amiens
80100 Abbeville|80230 Saint-Valery-sur-Somme|81000 Albi|81100 Castres|81600 Gaillac
82000 Montauban|82100 Castelsarrasin|83000 Toulon|83300 Draguignan|83400 Hyères
83500 La Seyne-sur-Mer|83600 Fréjus|83990 Saint-Tropez|84000 Avignon|84100 Orange
84160 Lourmarin|84200 Carpentras|84300 Cavaillon|85000 La Roche-sur-Yon
85100 Les Sables-d'Olonne|85330 Noirmoutier-en-l'Île|86000 Poitiers|86100 Châtellerault
86500 Montmorillon|87000 Limoges|87200 Saint-Junien|88000 Épinal|88100 Saint-Dié-des-Vosges
88400 Gérardmer|89000 Auxerre|89100 Sens|89200 Avallon|90000 Belfort
91000 Évry-Courcouronnes|91120 Palaiseau|91150 Étampes|91300 Massy|91400 Orsay
92000 Nanterre|92100 Boulogne-Billancourt|92130 Issy-les-Moulineaux|92200 Neuilly-sur-Seine
92300 Levallois-Perret|92400 Courbevoie|92500 Rueil-Malmaison|93100 Montreuil
93200 Saint-Denis|93300 Aubervilliers|94000 Créteil|94200 Ivry-sur-Seine|94300 Vincennes
94400 Vitry-sur-Seine|95000 Cergy|95100 Argenteuil|95200 Sarcelles|95300 Pontoise
95880 Enghien-les-Bains
"""


@lru_cache(maxsize=1)
def communes() -> tuple[tuple[str, str], ...]:
    """``(postcode, commune)`` pairs, in postcode order."""
    pairs: list[tuple[str, str]] = []
    for raw in _COMMUNES.replace("\n", "|").split("|"):
        entry = raw.strip()
        if entry:
            postcode, name = entry.split(" ", 1)
            pairs.append((postcode, name))
    return tuple(pairs)


__all__ = ["communes"]
