import sys
sys.path.append('.')
from pipeline.comparator import compare_fields, normalize_field

cases = [
    ('Weight decimals (15200 vs 15200.00)', {'gross_weight_kg': '15200 KG'}, {'gross_weight_kg': '15200.00 KG'}),
    ('Weight with comma decimal (15,200.50 KG vs 15200.5 KG)', {'gross_weight_kg': '15,200.50 KG'}, {'gross_weight_kg': '15200.5 KG'}),
    ('Weight MT vs KG (15.2 MT vs 15200 KG)', {'gross_weight_kg': '15.2 MT'}, {'gross_weight_kg': '15200 KG'}),
    ('Weight LBS vs KG (33510 LBS vs 15200 KG)', {'gross_weight_kg': '33510 LBS'}, {'gross_weight_kg': '15200 KG'}),
    ('Container count reverse format (40HC X 2 vs 2X40HC)', {'container_count': '40HC X 2'}, {'container_count': '2X40HC'}),
    ('Container count word (TWO CONTAINERS vs 2)', {'container_count': 'TWO (2) CONTAINERS'}, {'container_count': '2'}),
    ('Container count with size (2 X 40HQ vs 2)', {'container_count': '2 X 40HQ'}, {'container_count': '2'}),
    ('Shipper Acronym vs Full Name', {'shipper': 'APRIL'}, {'shipper': 'ASIA PACIFIC RESOURCES INTERNATIONAL HOLDINGS LTD'}),
    ('Shipper with P.O. Box & Address', {'shipper': 'APRIL TRADING PTE LTD, 80 MARINE PARADE ROAD #18-01'}, {'shipper': 'APRIL TRADING PTE LTD'}),
    ('Port with country (ROTTERDAM, NETHERLANDS vs NLRTM)', {'port_of_discharge': 'ROTTERDAM, NETHERLANDS'}, {'port_of_discharge': 'NLRTM'}),
    ('Port acronym (PKG vs PORT KLANG)', {'port_of_loading': 'PKG'}, {'port_of_loading': 'PORT KLANG'}),
    ('Notify Party SAME AS CONSIGNEE', {'notify_party': 'SAME AS CONSIGNEE'}, {'notify_party': 'SAME AS CONSIGNEE'}),
    ('Consignee TO ORDER OF SHIPPER vs TO ORDER', {'consignee': 'TO ORDER OF SHIPPER'}, {'consignee': 'TO ORDER'}),
    ('Missing field as N/A vs blank', {'shipper': 'N/A'}, {'shipper': ''}),
    ('Negative weight (-500 KG vs 500 KG)', {'gross_weight_kg': '-500 KG'}, {'gross_weight_kg': '500 KG'}),
    ('Zero containers (0 vs None)', {'container_count': '0'}, {'container_count': ''}),
    ('XSS injection in shipper', {'shipper': '<script>alert(1)</script>'}, {'shipper': 'Acme Corp'}),
    ('Unicode / Chinese characters in shipper', {'shipper': '中远海运集运 (COSCO SHIPPING)'}, {'shipper': 'COSCO SHIPPING'}),
]

print("=== LOGISTICS & EDGE CASE COMPARISON AUDIT ===")
for name, si, bl in cases:
    defects, is_missing, comps = compare_fields(si, bl)
    field = list(si.keys())[0]
    match = comps[field].get('match')
    norm_si = normalize_field(field, si[field])
    norm_bl = normalize_field(field, bl[field])
    res_str = "PASS" if match else "FAIL"
    print(f"[{res_str}] Test: {name}")
    print(f"   Raw SI: {si[field]!r} -> Norm: {norm_si!r}")
    print(f"   Raw BL: {bl[field]!r} -> Norm: {norm_bl!r}")
    print(f"   Match: {match} | Defect: {defects} | Missing: {is_missing}")
    print(f"   Reason: {comps[field].get('reason', '')}")
    print("-" * 65)
