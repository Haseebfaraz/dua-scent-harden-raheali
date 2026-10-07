"""Note-text mojibake repair. Port of the reference app's scripts/noteEncodingFixes.cjs.

The source spreadsheet stores some non-ASCII characters as the literal three characters "ï¿½"
(a UTF-8 replacement character decoded as Latin-1). Each entry below was reviewed by hand in the
reference app; corrections apply longest-first as global replacements so a longer phrase wins over
a shorter one it contains ("Green Matï¿½ Absolute" before "Matï¿½"). Text without the marker is
returned unchanged. Imports run every note string through fix_note_encoding so the mojibake is
never reintroduced.
"""

MOJIBAKE = "ï¿½"

CORRECTIONS = {
    "Matï¿½": "Maté",
    "Strawberry Purï¿½e": "Strawberry Purée",
    "Orcanoxï¿½": "Orcanox®",
    "Crï¿½me De Cassis": "Crème De Cassis",
    "Ambrofixï¿½": "Ambrofix®",
    "Raspberry Purï¿½e": "Raspberry Purée",
    "Crï¿½me Brï¿½lï¿½e": "Crème Brûlée",
    "Lab-Engineered Vanilla Pheraï¿½Moan": "Lab-Engineered Vanilla Phera'Moan",
    "Vanilla Musk Pheroï¿½moan Accord": "Vanilla Musk Phero'moan Accord",
    "Oud Assafiï¿½": "Oud Assafi®",  # LOW CONFIDENCE (reference app): could be an accent instead of a trademark
    "Vanilla Crï¿½me": "Vanilla Crème",
    "Up to the wearer to determine! Thatï¿½s the mystery of Mysterious\r\nElixir!": "Up to the wearer to determine! That's the mystery of Mysterious\r\nElixir!",
    "Rum Pure Jungle Essenceï¿½": "Rum Pure Jungle Essence®",
    "and Sicilian Citruses Fruit Mï¿½lange with Vanilla Extrait": "and Sicilian Citruses Fruit Mélange with Vanilla Extrait",
    "Arabian Taï¿½f Rose": "Arabian Taïf Rose",
    "Juicy Aï¿½ai": "Juicy Açaí",
    "Dyerï¿½s Greenweed": "Dyer's Greenweed",
    "Precious Woods and Melï¿½nge of Musks": "Precious Woods and Mélange of Musks",
    "Sicilian Citruses Fruit Mï¿½lange with Vanilla Extrait": "Sicilian Citruses Fruit Mélange with Vanilla Extrait",
    "Fougï¿½re Accord": "Fougère Accord",
    "Rose from Taï¿½if": "Rose from Ta'if",
    "Green Matï¿½ Absolute": "Green Maté Absolute",
    "ï¿½Starfishï¿½ Accord": "'Starfish' Accord",
    "Creamy Chai Lattï¿½": "Creamy Chai Latté",
    "and Matï¿½": "and Maté",
    "Yerba Matï¿½": "Yerba Maté",
    "Passion Fruit Purï¿½e": "Passion Fruit Purée",
    "Sweet Banana Purï¿½e": "Sweet Banana Purée",
    "Aï¿½ai Berry": "Açaí Berry",
    "Peach Purï¿½e": "Peach Purée",
    "Jalapeï¿½o": "Jalapeño",
    "Valencia Orange Crï¿½me": "Valencia Orange Crème",
    "Vanilla Pastry Crï¿½me": "Vanilla Pastry Crème",
    "Pineapple Pureï¿½": "Pineapple Purée",
    "Cherry Purï¿½e": "Cherry Purée",
    "Cupuaï¿½u": "Cupuaçu",
    "Sï¿½mores": "S'mores",
    "Pï¿½te Feuilletï¿½e": "Pâte Feuilletée",
    "Ultravanilï¿½": "Ultravanil®",  # LOW CONFIDENCE (reference app): could be "Ultravanille"
    "Tiramisï¿½ Accord": "Tiramisù Accord",
    "White Tiarï¿½ Flower": "White Tiaré Flower",
    "Musky Ambrofixï¿½": "Musky Ambrofix®",
    "Black Sï¿½sam Extract CO2": "Black Sésame Extract CO2",
    "Pralinï¿½": "Praliné",
    "Vanilla Crï¿½me Pastry": "Vanilla Crème Pastry",
    "Cï¿½dre-sur-Orris": "Cèdre-sur-Orris",
    "Powdered Confectionerï¿½s Sugar": "Powdered Confectioner's Sugar",
    "Burnt Crï¿½me Brï¿½lï¿½e": "Burnt Crème Brûlée",
    "Brï¿½lï¿½ed Vanilla Custard": "Brûléed Vanilla Custard",
    "Crï¿½me Dessert Pistache de Bronte": "Crème Dessert Pistache de Bronte",
    "Coconut Rapï¿½": "Coconut Rapé",
    "Ambroxï¿½ Super": "Ambrox® Super",  # LOW CONFIDENCE (reference app): could be "Ambroxan Super"
    "Vanilla Jungle Essenceï¿½": "Vanilla Jungle Essence®",
    "Vanilla Soufflï¿½ Accord": "Vanilla Soufflé Accord",
    "Crï¿½me De Coconut": "Crème De Coconut",
    "Chantilly Crï¿½me": "Chantilly Crème",
    "Piï¿½a Colada": "Piña Colada",
    "Warm Vanilla Crï¿½me": "Warm Vanilla Crème",
    "Cafï¿½ Au Lait": "Café Au Lait",
    "Benzoin Siam Resinoï¿½d": "Benzoin Siam Resinoid",
    "Cï¿½drat": "Cédrat",
    "Jasmine Sambac India Craftivityï¿½": "Jasmine Sambac India Craftivity®",
    "Cacao Blanc Peru Craftivityï¿½": "Cacao Blanc Peru Craftivity®",
    "Osmanthus China Craftivityï¿½": "Osmanthus China Craftivity®",
    "Red Velvet Crï¿½me": "Red Velvet Crème",
    "Cafï¿½ Arabica": "Café Arabica",
    "Amberxtremeï¿½": "Amberxtreme®",
    "Inc. Sinfonideï¿½": "Inc. Sinfonide®",
    "Crï¿½me de Cassis": "Crème de Cassis",
    "Blue Vanilla NaturePrintï¿½": "Blue Vanilla NaturePrint®",
    "Woody Citrus Pheroï¿½moanï¿½": "Woody Citrus Phero'moan®",
    "Citrus Musk Pheroï¿½moan Accord": "Citrus Musk Phero'moan Accord",
    "Lab Engineered Citrus Musk Pheraï¿½moan Accord": "Lab Engineered Citrus Musk Phera'moan Accord",
    "Woody Citrus Phereï¿½moan Accord": "Woody Citrus Phere'moan Accord",
    "Maple Caramel Crï¿½me": "Maple Caramel Crème",
    "Ambrexolideï¿½": "Ambrexolide®",
    "Lab-Engineered Citrus Musk Phero'moanï¿½": "Lab-Engineered Citrus Musk Phero'moan®",
}

_ORDERED_KEYS = sorted(CORRECTIONS, key=len, reverse=True)  # stable, like the JS sort


def fix_note_encoding(text):
    if not isinstance(text, str) or MOJIBAKE not in text:
        return text
    for bad in _ORDERED_KEYS:
        if bad in text:
            text = text.replace(bad, CORRECTIONS[bad])
    return text


if __name__ == "__main__":
    assert fix_note_encoding("Rose, Musk") == "Rose, Musk"
    assert fix_note_encoding(None) is None
    assert fix_note_encoding("Green Matï¿½ Absolute, Matï¿½") == "Green Maté Absolute, Maté"
    assert all(MOJIBAKE not in fix_note_encoding(k) for k in CORRECTIONS)
    print("note_encoding ok")
