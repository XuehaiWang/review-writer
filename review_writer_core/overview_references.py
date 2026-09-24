"""Optional author-supplied chemistry references, never source evidence."""
from pathlib import Path


def validate_reference(value: dict) -> dict:
    value = dict(value)
    reaction = value.get("kind") == "reaction"
    smiles = str(value.get("smiles") or "").strip()
    if reaction:
        parts = smiles.split(">")
        if len(parts) != 3 or not parts[0].strip() or not parts[2].strip():
            raise ValueError("反应式请填写完整 Reaction SMILES：反应物>>产物；不可只填写底物。")
        if any(not molecule.strip() for side in (parts[0], parts[2]) for molecule in side.split(".")):
            raise ValueError("反应式两侧不能包含空的分子项。")
        molecules = [s for side in parts for s in side.split(".") if s.strip()]
        if len(molecules) > 12:
            raise ValueError("单条反应参考最多包含 12 个分子，请选择代表性反应。")
    else:
        if not smiles and not str(value.get("name") or "").strip():
            raise ValueError("请提供物质名称或 SMILES，或移除空白参考。")
        molecules = [smiles] if smiles else []
    if molecules:
        from rdkit import Chem, rdBase
        with rdBase.BlockLogs():
            if any(Chem.MolFromSmiles(s) is None for s in molecules):
                raise ValueError("结构参考中存在无效 SMILES，请修正后生成。")
    value["smiles"] = smiles
    return value


def prepare_reference_images(references: list[dict], directory: Path) -> tuple[list[Path], str]:
    """Render exact 2D inputs as auxiliary images; do not infer products/names."""
    if not references:
        return [], ""
    import json
    from rdkit import Chem
    from rdkit.Chem import Draw, rdChemReactions
    directory.mkdir(parents=True, exist_ok=True)
    paths, descriptions = [], []
    for index, raw in enumerate(references):
        value = validate_reference(raw)
        smiles = value.get("smiles", "")
        if smiles:
            path = directory / f"author-reference-{index + 1}.png"
            if value["kind"] == "reaction":
                reaction = rdChemReactions.ReactionFromSmarts(smiles, useSmiles=True)
                Draw.ReactionToImage(reaction, subImgSize=(360, 260)).save(path)
            else:
                Draw.MolToImage(Chem.MolFromSmiles(smiles), size=(600, 400)).save(path)
            paths.append(path)
            value["reference_image"] = path.name
        descriptions.append(value)
    prompt = (
        "\nOptional author-supplied structure references (NOT verified literature evidence):\n"
        + json.dumps(descriptions, ensure_ascii=False)
        + "\nUse these auxiliary 2D drawings as structure references, not as layout templates. "
        "Respect substrate/product/catalyst roles; never infer a product or reaction from a single molecule. "
        "Names without drawings are text-only unless independently confirmed by the supplied project evidence. "
        "Do not override conflicting source-checked chemistry or present author references as published results. "
        "Do not repeat labels or explanations already shown elsewhere. No 3D or ball-and-stick structures."
    )
    return paths, prompt
