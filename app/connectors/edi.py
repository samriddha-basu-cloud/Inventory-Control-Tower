"""EDI (ANSI X12) translation architecture.

Supported transactions (simplified, documented mappings - real partner guides differ and are configurable here):
  846  Inventory Inquiry/Advice      → ExternalBalance records (partner-reported stock)
  856  Advance Ship Notice           → ShipmentDispatched event (+ line SKUs / PO reference / ETA)
  214  Carrier Shipment Status       → ShipmentDispatched / ShipmentDelayed / delivery status event

New transactions plug in through `EDI_HANDLERS`. This module only *translates text to canonical records/events*; it
does not open any connection. A real EDI/AS2/VAN link must be configured outside ICT before any partner data flows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SEGMENT_TERM = "~"
ELEMENT_SEP = "*"

# AT7 (shipment status) simplified mapping - configurable per trading partner
STATUS_CODE_MAP = {"AF": "DEPARTED", "X3": "ARRIVED_PICKUP", "X1": "ARRIVED_DELIVERY", "D1": "DELIVERED", "AG": "ETA_UPDATED", "SD": "DELAYED", "AA": "PICKUP_APPT"}
DELAY_REASON = {"AH": "Weather", "AI": "Mechanical breakdown", "A3": "Customs hold", "AJ": "Port congestion", "NA": ""}


@dataclass
class EdiResult:
    transaction: str
    control_number: str | None = None
    partner: str | None = None
    records: list = field(default_factory=list)
    events: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    segments: int = 0


class EdiError(ValueError):
    pass


def _segments(text: str) -> list[list[str]]:
    text = text.replace("\r", "").replace("\n", "").strip()
    if not text.startswith("ISA"):
        raise EdiError("Not an X12 interchange: missing ISA header.")
    sep = text[3] if len(text) > 3 else ELEMENT_SEP
    term = SEGMENT_TERM
    segs = [s.strip() for s in text.split(term) if s.strip()]
    return [s.split(sep) for s in segs]


def _date(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip()
    if len(v) == 8 and v.isdigit():
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    if len(v) == 6 and v.isdigit():
        return f"20{v[:2]}-{v[2:4]}-{v[4:]}"
    return v


def _el(seg: list[str], i: int, default=None):
    return seg[i] if len(seg) > i and seg[i] != "" else default


def parse_846(segs: list[list[str]], res: EdiResult) -> None:
    """LIN*1*UP*<sku>  QTY*33*<qty>*EA  REF*LT*<lot>  DTM*405*<date> ; SE closes the set. Partner from N1*SU/WH or ISA sender."""
    loc = None
    cur: dict | None = None
    for s in segs:
        tag = s[0]
        if tag == "N1" and _el(s, 1) in ("WH", "ST", "LW"):
            loc = _el(s, 4) or _el(s, 2)
        elif tag == "LIN":
            if cur:
                res.records.append(cur)
            sku = None
            for i in range(2, len(s) - 1, 2):
                if s[i] in ("UP", "VN", "BP", "SK", "IN"):
                    sku = s[i + 1]
                    break
            cur = {"entity": "external_balance", "system": res.partner or "3PL", "sku": sku, "location": loc, "qty": None, "uom": "EA", "lot": None, "as_of": None}
        elif tag == "QTY" and cur is not None:
            if _el(s, 1) in ("33", "17", "38"):
                try:
                    cur["qty"] = float(_el(s, 2, 0))
                    cur["uom"] = _el(s, 3, "EA")
                except ValueError:
                    res.warnings.append(f"Bad QTY value '{_el(s, 2)}' for {cur['sku']}")
        elif tag == "REF" and cur is not None and _el(s, 1) == "LT":
            cur["lot"] = _el(s, 2)
        elif tag == "DTM" and cur is not None:
            cur["as_of"] = _date(_el(s, 2))
    if cur:
        res.records.append(cur)
    for r in res.records:
        if not r["sku"] or r["qty"] is None:
            res.warnings.append("846 line missing SKU or quantity")


def parse_856(segs: list[list[str]], res: EdiResult) -> None:
    """BSN*00*<shipment>*<date>  PRF*<po>  DTM*017*<eta>  LIN..UP*<sku>  SN1**<qty>*EA  TD5 (mode/carrier)  REF*BM*<BOL>"""
    ship = {"shipment_no": None, "po": None, "eta": None, "carrier": None, "bol": None, "lines": [], "ship_date": None, "mode": None}
    cur_sku = None
    for s in segs:
        tag = s[0]
        if tag == "BSN":
            ship["shipment_no"], ship["ship_date"] = _el(s, 2), _date(_el(s, 3))
        elif tag == "PRF":
            ship["po"] = _el(s, 1)
        elif tag == "DTM" and _el(s, 1) in ("017", "002", "011"):
            if _el(s, 1) == "017":
                ship["eta"] = _date(_el(s, 2))
            elif _el(s, 1) == "011":
                ship["ship_date"] = _date(_el(s, 2))
        elif tag == "TD5":
            ship["carrier"] = _el(s, 3)
            ship["mode"] = {"J": "ROAD", "A": "AIR", "O": "SEA", "R": "RAIL"}.get(_el(s, 4), None)
        elif tag == "REF" and _el(s, 1) == "BM":
            ship["bol"] = _el(s, 2)
        elif tag == "LIN":
            cur_sku = None
            for i in range(2, len(s) - 1, 2):
                if s[i] in ("UP", "VN", "BP", "SK", "IN"):
                    cur_sku = s[i + 1]
                    break
        elif tag == "SN1" and cur_sku:
            try:
                ship["lines"].append({"sku": cur_sku, "qty": float(_el(s, 2, 0))})
            except ValueError:
                res.warnings.append(f"Bad SN1 quantity for {cur_sku}")
    if not ship["shipment_no"]:
        res.warnings.append("856 without BSN shipment identifier")
    res.events.append({"event_type": "ShipmentDispatched", "payload": ship})


def parse_214(segs: list[list[str]], res: EdiResult) -> None:
    """B10*<ref>*<shipment>*<scac>  AT7*<status>*<reason>***<date>*<time>  DTM*<eta>  MS1 (location)"""
    shipment, carrier, ref = None, None, None
    for s in segs:
        if s[0] == "B10":
            ref, shipment, carrier = _el(s, 1), _el(s, 2), _el(s, 3)
    eta = None
    for s in segs:
        if s[0] == "DTM" and _el(s, 1) in ("017", "AA1", "ETA"):
            eta = _date(_el(s, 2))
    for s in segs:
        if s[0] != "AT7":
            continue
        status = STATUS_CODE_MAP.get(_el(s, 1), _el(s, 1))
        reason = DELAY_REASON.get(_el(s, 2, "NA"), _el(s, 2))
        when = _date(_el(s, 5))
        payload = {"shipment_no": shipment or ref, "carrier": carrier, "status": status, "reason": reason, "date": when, "eta": eta}
        if status in ("DELAYED",) or (reason and status == "ETA_UPDATED"):
            res.events.append({"event_type": "ShipmentDelayed", "payload": payload})
        elif status == "DEPARTED":
            res.events.append({"event_type": "ShipmentDispatched", "payload": {**payload, "lines": []}})
        else:
            res.events.append({"event_type": "ShipmentStatus", "payload": payload})
    if not res.events:
        res.warnings.append("214 without AT7 status segments")


EDI_HANDLERS = {"846": parse_846, "856": parse_856, "214": parse_214}


def translate(text: str, partner: str | None = None) -> EdiResult:
    segs = _segments(text)
    isa = next((s for s in segs if s[0] == "ISA"), None)
    st = next((s for s in segs if s[0] == "ST"), None)
    if not st:
        raise EdiError("Missing ST segment: cannot identify the transaction set.")
    tx = _el(st, 1)
    if tx not in EDI_HANDLERS:
        raise EdiError(f"EDI {tx} is not mapped. Supported: {', '.join(sorted(EDI_HANDLERS))}. Add a handler to EDI_HANDLERS to extend.")
    res = EdiResult(transaction=tx, control_number=_el(st, 2), partner=partner or (_el(isa, 6, "").strip() if isa else None), segments=len(segs))
    se = next((s for s in segs if s[0] == "SE"), None)
    if se and _el(se, 1) and _el(se, 1).isdigit():
        expected = int(_el(se, 1))
        actual = segs.index(se) - segs.index(st) + 1
        if expected != actual:
            res.warnings.append(f"SE01 segment count {expected} differs from actual {actual}")
    elif not se:
        res.warnings.append("Missing SE trailer")
    EDI_HANDLERS[tx](segs, res)
    return res


SAMPLES = {
    "846": "ISA*00*          *00*          *ZZ*COLDSTORE3PL   *ZZ*ICTDEMO        *260921*0900*U*00401*000000101*0*P*>~GS*IB*COLDSTORE3PL*ICTDEMO*20260921*0900*101*X*004010~"
           "ST*846*0001~BIA*00*DD*ADV101*20260921~N1*WH*Delhi 3PL Cold Store*92*PHM-3PL-N~LIN*1*UP*PHM-TAB-METF500~QTY*33*7200*EA~REF*LT*TRACE-FG-PHA-01~DTM*405*20260921~"
           "LIN*2*UP*PHM-INJ-INSUL~QTY*33*640*EA~SE*10*0001~GE*1*101~IEA*1*000000101~",
    "856": "ISA*00*          *00*          *ZZ*HYDAPILABS     *ZZ*ICTDEMO        *260921*0900*U*00401*000000102*0*P*>~GS*SH*HYDAPILABS*ICTDEMO*20260921*0900*102*X*004010~"
           "ST*856*0002~BSN*00*ASN-778812*20260921*0900~DTM*011*20260920~DTM*017*20260930~TD5**2*BLUEDART*J~REF*BM*BOL-9931~HL*1**S~PRF*PO-PHM-00001~HL*2*1*I~"
           "LIN**UP*PHM-API-METF~SN1**1000*EA~SE*13*0002~GE*1*102~IEA*1*000000102~",
    "214": "ISA*00*          *00*          *ZZ*OCEANICSCAC    *ZZ*ICTDEMO        *260921*0900*U*00401*000000103*0*P*>~GS*QM*OCEANICSCAC*ICTDEMO*20260921*0900*103*X*004010~"
           "ST*214*0003~B10*REF77*SHP-AUT-00012*OCNC~AT7*SD*AJ***20260921*0900~DTM*017*20261003~SE*5*0003~GE*1*103~IEA*1*000000103~",
}
