"""Master data: items, locations, suppliers, customers, carriers, relationships."""
from ..extensions import db
from .base import CanonicalMixin, utcnow


class ProductFamily(CanonicalMixin, db.Model):
    __tablename__ = "product_family"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120))
    category = db.Column(db.String(80))


class Item(CanonicalMixin, db.Model):
    """SKU / item master (Item = SKU = Product in ICT's canonical model)."""
    __tablename__ = "item"
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(60), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255))
    family_code = db.Column(db.String(40), index=True)
    category = db.Column(db.String(80), index=True)
    industry = db.Column(db.String(30), index=True)
    business_unit = db.Column(db.String(60), index=True)
    item_type = db.Column(db.String(20), default="FG")        # RAW / COMPONENT / SEMI / FG / SPARE / PACKAGING
    uom = db.Column(db.String(12), default="EA")
    pack_size = db.Column(db.Float, default=1)
    case_qty = db.Column(db.Float)
    moq = db.Column(db.Float)
    order_multiple = db.Column(db.Float)
    unit_cost = db.Column(db.Float)
    selling_price = db.Column(db.Float)
    currency = db.Column(db.String(3), default="INR")
    shelf_life_days = db.Column(db.Integer)
    abc_class = db.Column(db.String(1))                        # manual/master override (analytics computes its own)
    xyz_class = db.Column(db.String(1))
    criticality = db.Column(db.String(10), default="Medium")   # Critical / High / Medium / Low
    ved = db.Column(db.String(1))                              # Vital / Essential / Desirable
    sde = db.Column(db.String(1))                              # Scarce / Difficult / Easy
    hazardous = db.Column(db.Boolean, default=False)
    temp_requirement = db.Column(db.String(20), default="AMBIENT")
    country_of_origin = db.Column(db.String(60))
    weight_kg = db.Column(db.Float, default=1.0)
    volume_m3 = db.Column(db.Float, default=0.001)
    lot_tracked = db.Column(db.Boolean, default=False)
    serial_tracked = db.Column(db.Boolean, default=False)
    target_service_level = db.Column(db.Float)
    lifecycle_status = db.Column(db.String(12), default="ACTIVE")  # NPI / ACTIVE / EOL / OBSOLETE
    eol_date = db.Column(db.Date)
    revision = db.Column(db.String(10))
    superseded_by_sku = db.Column(db.String(60))               # ECO successor
    eco_effective_date = db.Column(db.Date)
    eco_reworkable = db.Column(db.Boolean, default=False)
    substitute_group = db.Column(db.String(40))
    markdown_pct = db.Column(db.Float, default=0.0)
    line_stop_cost_per_unit = db.Column(db.Float)               # production risk per missing unit (critical parts)


class ItemUom(CanonicalMixin, db.Model):
    """Item-specific pack conversion, e.g. 1 CASE = 24 EA."""
    __tablename__ = "item_uom"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    from_uom = db.Column(db.String(12), nullable=False)
    to_uom = db.Column(db.String(12), nullable=False)
    factor = db.Column(db.Float, nullable=False)


class Location(CanonicalMixin, db.Model):
    """Any node: plant, warehouse, DC, store, dark store, MFC, 3PL, line-side..."""
    __tablename__ = "location"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    loc_type = db.Column(db.String(20), default="WAREHOUSE")   # PLANT / CDC / RDC / WAREHOUSE / STORE / DARK_STORE / MFC / 3PL / LINE_SIDE
    echelon = db.Column(db.Integer, default=2)                 # 1 = closest to supply
    parent_id = db.Column(db.Integer, db.ForeignKey("location.id"))   # default upstream replenishment source
    region = db.Column(db.String(60), index=True)
    country = db.Column(db.String(60))
    city = db.Column(db.String(60))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    industry = db.Column(db.String(30), index=True)
    business_unit = db.Column(db.String(60))
    capacity_units = db.Column(db.Float)
    capacity_m3 = db.Column(db.Float)
    is_3pl = db.Column(db.Boolean, default=False)
    temp_capable = db.Column(db.String(40), default="AMBIENT")
    channel = db.Column(db.String(20))                         # STORE / ONLINE / B2B
    parent = db.relationship("Location", remote_side=[id])


class Supplier(CanonicalMixin, db.Model):
    __tablename__ = "supplier"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    tier = db.Column(db.Integer, default=1)
    country = db.Column(db.String(60))
    region = db.Column(db.String(60))
    city = db.Column(db.String(60))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    industry = db.Column(db.String(30), index=True)
    payment_terms_days = db.Column(db.Integer, default=45)
    currency = db.Column(db.String(3), default="INR")
    geo_risk = db.Column(db.Float, default=0.1)                # 0..1 configured country / lane risk
    capacity_units_week = db.Column(db.Float)
    quality_rejection_rate = db.Column(db.Float, default=0.01)


class Customer(CanonicalMixin, db.Model):
    __tablename__ = "customer"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    segment = db.Column(db.String(40))
    priority = db.Column(db.Integer, default=3)                # 1 = highest
    contract_priority = db.Column(db.Boolean, default=False)
    region = db.Column(db.String(60), index=True)
    country = db.Column(db.String(60))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    industry = db.Column(db.String(30))


class Carrier(CanonicalMixin, db.Model):
    __tablename__ = "carrier"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(120))
    mode = db.Column(db.String(12), default="ROAD")            # ROAD / RAIL / SEA / AIR


class ItemSupplier(CanonicalMixin, db.Model):
    """Sourcing relationship: which supplier can supply which item, on what static terms."""
    __tablename__ = "item_supplier"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False, index=True)
    is_primary = db.Column(db.Boolean, default=True)
    lead_time_days = db.Column(db.Float)                       # STATIC (master) lead time
    moq = db.Column(db.Float)
    order_multiple = db.Column(db.Float)
    price = db.Column(db.Float)
    capacity_units_week = db.Column(db.Float)
    mode = db.Column(db.String(12), default="ROAD")
    lane = db.Column(db.String(120))
    origin_port = db.Column(db.String(60))
    item = db.relationship("Item")
    supplier = db.relationship("Supplier")


class ItemLocationSource(CanonicalMixin, db.Model):
    """Item-location upstream source when replenished internally (plant -> DC -> store) rather than by a supplier."""
    __tablename__ = "item_location_source"
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"), nullable=False, index=True)
    source_location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    lead_time_days = db.Column(db.Float)


class BomLine(CanonicalMixin, db.Model):
    __tablename__ = "bom_line"
    id = db.Column(db.Integer, primary_key=True)
    parent_item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    component_item_id = db.Column(db.Integer, db.ForeignKey("item.id"), nullable=False, index=True)
    qty_per = db.Column(db.Float, default=1.0)
    revision = db.Column(db.String(10))
    valid_from = db.Column(db.Date)
    valid_to = db.Column(db.Date)
    is_alternate = db.Column(db.Boolean, default=False)
    alt_group = db.Column(db.String(40))
    scrap_pct = db.Column(db.Float, default=0.0)


class CalendarEvent(CanonicalMixin, db.Model):
    __tablename__ = "calendar_event"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    event_type = db.Column(db.String(20))   # HOLIDAY / SHUTDOWN / PEAK / MAINTENANCE
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))


class Promotion(CanonicalMixin, db.Model):
    __tablename__ = "promotion"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"))
    family_code = db.Column(db.String(40))
    location_id = db.Column(db.Integer, db.ForeignKey("location.id"))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    uplift_pct = db.Column(db.Float, default=0.0)
    promo_type = db.Column(db.String(30))
