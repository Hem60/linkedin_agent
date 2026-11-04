"""The real products, and the certifications that actually exist.

The brand strategy asks for this before anything else: "give the generator a
verified product data source ... so it can create varied content without making
up product details." Without one, the claim check in cardwriter can only
compare a card against a post, and the post is written by the same model -- so
a card inherits whatever the draft invented. Asked about biodegradable
packaging, the drafting model wrote "organic" and "certified" into a post and
the card carried them through honestly, because the post said so.

This is the floor under that. A photograph here is of the product itself, so a
card showing it is not a generated approximation of something similar. A
certification here is one the company holds, so naming it is reporting rather
than inventing.

peanut-product2 is deliberately absent and has been removed: it is a sample
shot on a workbench with a handwritten "Bold" label in frame. The brief asks
for premium minimal commercial imagery, and a card is not the place for
somebody's QC note.

Material is only recorded where it was stated. The bagasse clamshell is the
one product confirmed as bagasse; the others carry "tableware" because nobody
has said what they are made of, and the folder being called bio- is not a
statement about material. The 9x6 comes in both, which is why the material is
in that one's name and not in the others.

Only what has been seen goes in. Entries are deliberately absent for the
photographs nobody has identified yet: an unlabelled file is not product data,
and guessing what a picture shows is the failure this module exists to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PRODUCTS_DIR = Path(__file__).resolve().parent / "static" / "products"

# Certifications the company holds, evidenced by the documents in the asset
# library. These are the only ones a card may name.
CERTIFICATIONS = ("APEDA", "CIPET", "FDA", "FSSAI", "GST", "IEC", "ISO", "MSME")


@dataclass(frozen=True)
class Product:
    """One real product, with a photograph of that product."""

    key: str
    name: str
    line: str                       # bagasse | groundnut | quartz
    photo: str                      # filename under static/products
    describes: str = ""             # what the photograph actually shows
    keywords: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> Path:
        return PRODUCTS_DIR / self.photo

    @property
    def url(self) -> str:
        return f"/static/products/{self.photo}"


# Identified by looking at the photographs. Anything not looked at is left out
# rather than guessed -- see the module docstring.
PRODUCTS: tuple[Product, ...] = (
    Product(
        key="plate-3cp",
        name="3-Compartment Round Plate",
        line="tableware",   # material not stated,
        photo="bio-product1.jpg",
        describes="two moulded fibre round plates with three compartments, "
                  "plain white background",
        keywords=("compartment", "compartments", "plate", "plates", "thali"),
    ),
    Product(
        key="plate-plain",
        name="Plain Round Plate",
        line="tableware",   # material not stated,
        photo="bio-product4.jpg",
        describes="two plain moulded fibre round plates, plain white background",
        keywords=("plate", "plates", "crockery"),
    ),
    Product(
        key="clamshell-8x8-3cp",
        name='8" x 8" 3-Compartment Clamshell',
        line="tableware",   # material not stated; see module note,
        photo="bio-product5.jpg",
        describes="square hinged clamshell with three compartments, open, "
                  "plain white background",
        keywords=("clamshell", "clamshells", "hinged", "takeaway", "8x8"),
    ),
    Product(
        key="clamshell-6x6-3cp",
        name='6" x 6" 3-Compartment Clamshell',
        line="tableware",   # material not stated; see module note,
        photo="bio-product6.jpg",
        describes="square hinged clamshell with three compartments, deeper "
                  "wells, open, plain white background",
        keywords=("clamshell", "clamshells", "hinged", "6x6"),
    ),
    Product(
        key="clamshell-9x6-single",
        name='9" x 6" Single-Compartment Clamshell',
        line="tableware",   # material not stated; see module note,
        photo="bio-product7.jpg",
        describes="hinged single-compartment clamshell, open, plain white "
                  "background",
        keywords=("clamshell", "clamshells", "hinged", "9x6", "single"),
    ),
    Product(
        key="clamshell-9x6-bagasse",
        name='9" x 6" Bagasse Clamshell',
        line="bagasse",
        photo="bio-product8.jpg",
        describes="rectangular hinged clamshell in natural beige moulded "
                  "fibre, open, plain white background",
        keywords=("bagasse", "clamshell", "clamshells", "hinged", "9x6"),
    ),
    Product(
        key="groundnut-java",
        name="Java Groundnut Kernels",
        line="groundnut",
        photo="peanut-product3.jpg",
        describes="shelled red-skin groundnut kernels heaped on a plain white "
                  "surface",
        keywords=("java", "kernel", "kernels", "shelled", "blanched"),
    ),
    Product(
        key="groundnut-inshell",
        name="In-Shell Groundnuts",
        line="groundnut",
        photo="peanut-product1.jpg",
        describes="raw in-shell groundnuts scattered on a plain white surface",
        keywords=("peanut", "peanuts", "groundnut", "groundnuts"),
    ),
)

BY_KEY = {p.key: p for p in PRODUCTS}


def match(text: str) -> Product | None:
    """The catalogued product a piece of text is most plainly about.

    Deliberately literal: a keyword has to appear. A fuzzy match that returns
    a plate for a post about groundnuts would put the wrong product in front
    of a buyer, which is worse than returning nothing and letting the caller
    fall back to a generated picture.
    """
    low = text.lower()
    best: tuple[int, Product] | None = None
    for product in PRODUCTS:
        hits = sum(1 for word in product.keywords if word in low)
        if hits and (best is None or hits > best[0]):
            best = (hits, product)
    return best[1] if best else None


# Words that say nothing about which product this is. "export" was a keyword
# on the groundnuts and duly matched a post about biodegradable packaging,
# which would have put the wrong product in front of a buyer -- the precise
# failure the literal matcher was supposed to prevent. A keyword has to name
# the thing, not the trade it travels in.
_TOO_GENERIC = frozenset({
    "export", "exports", "import", "imports", "raw", "shell", "bulk",
    "product", "products", "goods", "packaging", "material", "materials",
    "shipment", "shipments", "quality", "eco", "green", "sustainable",
})


def photo_bytes(product: Product) -> bytes | None:
    """The product's photograph, or None when the file is missing."""
    try:
        return product.path.read_bytes()
    except OSError:
        return None


def supported_claim(text: str) -> bool:
    """Whether a certification named in `text` is one that actually exists."""
    low = text.lower()
    named = [c for c in CERTIFICATIONS if c.lower() in low]
    return bool(named)


# Posts that name no product at all
#
# "Biodegradable things", "sustainable packaging", "eco-friendly tableware" --
# these name a category, not an item, and the matcher rightly returns nothing.
# That left cards with no picture whenever generation was also unavailable.
#
# The obvious answer is a photograph of the whole range, and the one in the
# asset library is the wrong one: it shows cups, kraft bowls and paper boxes
# that are not in this catalogue, so a card carrying it would advertise
# products that may not exist. Showing one real item instead says less, but
# everything it says is true.
_CATEGORY_WORDS = (
    "tableware", "crockery", "disposable", "disposables", "foodservice",
    "food service", "catering", "takeaway", "packaging", "biodegradable",
    "compostable", "eco-friendly", "sustainable",
)

# The item shown when a post is about the range rather than a product. The
# bagasse clamshell: the one product whose material has actually been stated.
REPRESENTATIVE = "clamshell-9x6-bagasse"


def representative(text: str) -> Product | None:
    """A real product to stand for the range, when a post names no item."""
    low = text.lower()
    if any(word in low for word in _CATEGORY_WORDS):
        return BY_KEY.get(REPRESENTATIVE)
    return None
