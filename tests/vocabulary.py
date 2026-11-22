"""A few MeSH descriptors in NLM's own XML format, for the concept and search tests (ADR-008), so they
never reach the network."""

from pathlib import Path
from xml.sax.saxutils import escape

from trialwatch.vocab import load as vocab_loading

DESCRIPTORS = [
    # ui, name, tree numbers, other entry terms
    ("D009750", "Nutritional and Metabolic Diseases", ["C18"], []),
    ("D008659", "Metabolic Diseases", ["C18.452"], []),
    ("D044882", "Glucose Metabolism Disorders", ["C18.452.394"], []),
    ("D004700", "Endocrine System Diseases", ["C19"], []),
    ("D003920", "Diabetes Mellitus", ["C18.452.394.750", "C19.246"], []),
    ("D003924", "Diabetes Mellitus, Type 2", ["C18.452.394.750.149", "C19.246.300"],
     ["NIDDM", "Type 2 Diabetes", "Adult-Onset Diabetes Mellitus"]),
    ("D003922", "Diabetes Mellitus, Type 1", ["C18.452.394.750.124", "C19.246.267"], ["IDDM", "Type 1 Diabetes"]),
    ("D003919", "Diabetes Insipidus", ["C19.700.355.179"], []),
    ("D048909", "Diabetes Complications", ["C19.246.099"], []),
    ("D003930", "Diabetic Retinopathy", ["C19.246.099.500.382", "C11.768.257"], []),
    ("D009369", "Neoplasms", ["C04"], ["Cancer", "Tumors", "Tumor", "Neoplasia"]),
    ("D009371", "Neoplasms by Site", ["C04.588"], []),
    ("D001943", "Breast Neoplasms", ["C04.588.180"], ["Breast Cancer", "Breast Tumors"]),
    ("D015179", "Colorectal Neoplasms", ["C04.588.274.476.411.307"], ["Colorectal Cancer"]),
    ("D009203", "Myocardial Infarction", ["C14.280.647.500"], ["Heart Attack", "Heart Attacks"]),
    ("D001249", "Asthma", ["C08.127.108"], []),
    ("D003550", "Cystic Fibrosis", ["C16.320.190", "C08.381.187"], ["Mucoviscidosis"]),
    ("D008379", "Marburg Virus Disease", ["C01.925.782.580.200.500"], ["Marburg"]),
    # not a disease: left out by the loader
    ("D000001", "Calcimycin", ["D03.633.100.221.173"], []),
]


def write_mesh(folder: Path) -> Path:
    records = []
    for ui, name, tree_numbers, terms in DESCRIPTORS:
        numbers = ""
        for tree_number in tree_numbers:
            numbers += f"<TreeNumber>{tree_number}</TreeNumber>"
        term_list = f"<Term><String>{escape(name)}</String></Term>"
        for term in terms:
            term_list += f"<Term><String>{escape(term)}</String></Term>"
        records.append(
            f'<DescriptorRecord DescriptorClass = "1"><DescriptorUI>{ui}</DescriptorUI>'
            f"<DescriptorName><String>{escape(name)}</String></DescriptorName>"
            # a qualifier's name is a String too: not a term
            f"<AllowableQualifiersList><AllowableQualifier><QualifierReferredTo><QualifierName>"
            f"<String>therapy</String></QualifierName></QualifierReferredTo></AllowableQualifier></AllowableQualifiersList>"
            f"<TreeNumberList>{numbers}</TreeNumberList>"
            f'<ConceptList><Concept PreferredConceptYN="Y"><TermList>{term_list}</TermList></Concept></ConceptList>'
            f"</DescriptorRecord>"
        )
    path = folder / "desc2026.xml"
    path.write_text('<?xml version="1.0"?>\n<DescriptorRecordSet LanguageCode = "eng">\n' + "\n".join(records)
                    + "\n</DescriptorRecordSet>\n", encoding="utf-8")
    return path


def load_mesh(conn, folder: Path):
    """MeSH loaded from the descriptors above, as `trialwatch vocab load --from` would."""
    return vocab_loading.load_mesh(conn, None, write_mesh(folder))
