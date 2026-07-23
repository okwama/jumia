"""Typed model of one Upload_Template.xlsx row. See Readme.md #6, #11.

Field order here is documentation, not the export column order -- the
export writer derives column order from the real template's header row
at runtime (Readme.md #14), not from this model, since Jumia can change
the template without notice.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    Name: str
    Name_AR: str | None = None
    Name_FR: str | None = None
    Description: str | None = None
    Description_AR: str | None = None
    Description_FR: str | None = None
    SellerSKU: str
    ParentSKU: str | None = None
    Brand: str | None = None
    PrimaryCategory: str | None = None
    AdditionalCategory: str | None = None
    GTIN_Barcode: str | None = None
    Price_KES: float | None = None
    Sale_Price_KES: float | None = None
    Sale_Price_Start_At: str | None = None
    Sale_Price_End_At: str | None = None
    Stock: int | None = None

    # Category-conditional attributes (Readme.md #7), columns R-BH
    battery_capacity: str | None = None
    connection_gender: str | None = None
    cpu_manufacturer: str | None = None
    graphics_memory: str | None = None
    memory_technology: str | None = None
    panel_type: str | None = None
    processor_type: str | None = None
    storage_capacity: str | None = None
    variation: str | None = None
    certifications: str | None = None
    color: str | None = None
    color_AR: str | None = None
    color_FR: str | None = None
    color_family: str | None = None
    display_resolution: str | None = None
    display_size: str | None = None
    hdd_size: str | None = None
    main_material: str | None = None
    manufacturer_txt: str | None = None
    material_family: str | None = None
    memory_capacity: str | None = None
    model: str | None = None
    modem_type: str | None = None
    mount_type: str | None = None
    note: str | None = None
    package_content: str | None = None
    package_content_AR: str | None = None
    package_content_FR: str | None = None
    plug_type: str | None = None
    product_line: str | None = None
    product_measures: str | None = None
    product_warranty: str | None = None
    product_weight: float | None = None
    production_country: str | None = None
    short_description: str | None = None
    short_description_AR: str | None = None
    short_description_FR: str | None = None
    system_memory: str | None = None
    voltage: str | None = None
    warranty_address: str | None = None
    warranty_duration: str | None = None
    warranty_type: str | None = None
    youtube_id: str | None = None

    # Images, columns BI-BP
    MainImage: str | None = None
    Image2: str | None = None
    Image3: str | None = None
    Image4: str | None = None
    Image5: str | None = None
    Image6: str | None = None
    Image7: str | None = None
    Image8: str | None = None
