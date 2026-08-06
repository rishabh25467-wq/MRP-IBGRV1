"""
SAP Business ByDesign BOM Extraction Agent
Combines two OData services to get complete BOM data with all hierarchy levels and component details
"""

import requests
import json
from requests.auth import HTTPBasicAuth
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SAP_BOM_Extractor:
    """
    Extracts Production Bill of Material from SAP BYD using dual OData services:
    - bom_service: BOM hierarchy structure
    - bom_service1: Component details (Quantity, UOM, ECO)
    """
    
    def __init__(self, instance_url, username, password):
        self.instance_url = instance_url
        self.username = username
        self.password = password
        self.base_url_hierarchy = f"{instance_url}/sap/byd/odata/cust/v1/bom_service"
        self.base_url_details = f"{instance_url}/sap/byd/odata/cust/v1/bom_service1"
        self.headers = {"Accept": "application/json"}
        self.auth = HTTPBasicAuth(username, password)
        self.material_cache = {}  # Cache MaterialUUID → Material ID
    
    def convert_sap_date(self, sap_date_str):
        """Convert SAP /Date(timestamp)/ to ISO format"""
        if not sap_date_str or "/Date(" not in sap_date_str:
            return None
        try:
            timestamp = int(sap_date_str.split("(")[1].split(")")[0]) / 1000
            return datetime.fromtimestamp(timestamp).isoformat()
        except:
            return None
    
    def get_bom_hierarchy(self):
        """
        Query bom_service to get BOM structure:
        BOM → ItemGroups → ItemGroupItems (ObjectIDs only)
        """
        url = f"{self.base_url_hierarchy}/ProductionBillOfMaterialCollection"
        
        params = {
            "$expand": "ProductionBillOfMaterialItemGroup1/ProductionBillOfMaterialItemGroupItem",
            "$format": "json"
        }
        
        try:
            response = requests.get(
                url,
                params=params,
                headers=self.headers,
                auth=self.auth,
                timeout=60
            )
            
            if response.status_code == 200:
                data = response.json()
                hierarchy = {}
                
                for bom in data.get('d', {}).get('results', []):
                    bom_id = bom.get("ID")
                    
                    hierarchy[bom_id] = {
                        "bom_id": bom_id,
                        "object_id": bom.get("ObjectID"),
                        "uuid": bom.get("UUID"),
                        "item_groups": {}
                    }
                    
                    # Parse ItemGroups
                    for item_group in bom.get('ProductionBillOfMaterialItemGroup1', []):
                        group_id = item_group.get("BillOfMaterialItemGroupID")
                        
                        hierarchy[bom_id]["item_groups"][group_id] = {
                            "group_id": group_id,
                            "group_number": item_group.get("ID"),
                            "start_date": self.convert_sap_date(item_group.get("StartDate")),
                            "end_date": self.convert_sap_date(item_group.get("EndDate")),
                            "item_ids": []
                        }
                        
                        # Store ItemGroupItem ObjectIDs for later lookup
                        for item in item_group.get('ProductionBillOfMaterialItemGroupItem', []):
                            hierarchy[bom_id]["item_groups"][group_id]["item_ids"].append(
                                item.get("ObjectID")
                            )
                
                return {"status": "success", "data": hierarchy}
            else:
                return {"status": "error", "message": f"HTTP {response.status_code}"}
        
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    def get_component_details(self):
        """
        Query bom_service1 to get component details:
        - Quantity
        - Unit of Measure (unitCode)
        - Material UUID
        - Engineering Change Order ID
        """
        url = f"{self.base_url_details}/ProductionBillOfMaterialItemGroupItemChangeStateCollection"
        
        params = {
            "$expand": "ProductionBillOfMaterialAssignedVariant",
            "$format": "json"
        }
        
        try:
            response = requests.get(
                url,
                params=params,
                headers=self.headers,
                auth=self.auth,
                timeout=60
            )
            
            if response.status_code == 200:
                data = response.json()
                details = {}
                
                for change_state in data.get('d', {}).get('results', []):
                    eco_id = change_state.get("EngineeringChangeOrderID")
                    object_id = change_state.get("ObjectID")
                    
                    # Parse assigned variants (contains Quantity, UOM)
                    variants = []
                    for variant in change_state.get('ProductionBillOfMaterialAssignedVariant', []):
                        variants.append({
                            "quantity": variant.get("Quantity"),
                            "unit_code": variant.get("unitCode"),
                            "material_uuid": variant.get("MaterialUUID"),
                            "internal_id": variant.get("InternalID"),
                            "variant_id": variant.get("ID")
                        })
                    
                    details[object_id] = {
                        "object_id": object_id,
                        "eco_id": eco_id,
                        "material_uuid": change_state.get("MaterialUUID"),
                        "quantity_fixed": change_state.get("QuantityFixedIndicator"),
                        "deleted": change_state.get("DeletedIndicator"),
                        "variants": variants
                    }
                
                return {"status": "success", "data": details}
            else:
                return {"status": "error", "message": f"HTTP {response.status_code}"}
        
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    def resolve_material_uuid(self, material_uuid):
        """
        Lookup Material ID from UUID
        Can be enhanced to query Material Master if available
        """
        if material_uuid in self.material_cache:
            return self.material_cache[material_uuid]
        
        # For now, return UUID as placeholder
        # In production, query Material Master via c4codata endpoint
        self.material_cache[material_uuid] = material_uuid
        return material_uuid
    
    def extract_complete_bom(self):
        """
        Main extraction: Combine hierarchy + component details
        Returns complete BOM with all levels and quantities
        """
        
        # Step 1: Get hierarchy
        logger.info("Fetching BOM hierarchy from bom_service...")
        hierarchy_result = self.get_bom_hierarchy()
        if hierarchy_result["status"] != "success":
            return hierarchy_result
        
        hierarchy = hierarchy_result["data"]
        
        # Step 2: Get component details
        logger.info("Fetching component details from bom_service1...")
        details_result = self.get_component_details()
        if details_result["status"] != "success":
            return details_result
        
        details = details_result["data"]
        
        # Step 3: Merge hierarchy + details
        logger.info("Merging hierarchy and component details...")
        
        output = {
            "extraction_date": datetime.now().isoformat(),
            "status": "success",
            "bom_count": len(hierarchy),
            "boms": []
        }
        
        for bom_id, bom_data in hierarchy.items():
            bom_record = {
                "bom_id": bom_id,
                "object_id": bom_data["object_id"],
                "groups": []
            }
            
            total_components = 0
            
            for group_id, group_data in bom_data["item_groups"].items():
                group_record = {
                    "group_id": group_id,
                    "group_number": group_data["group_number"],
                    "start_date": group_data["start_date"],
                    "end_date": group_data["end_date"],
                    "components": []
                }
                
                # Look up each ItemGroupItem in component details
                for item_id in group_data["item_ids"]:
                    if item_id in details:
                        detail = details[item_id]
                        
                        # Extract first variant (usually only one per change state)
                        if detail["variants"]:
                            variant = detail["variants"][0]
                            component = {
                                "object_id": item_id,
                                "eco_id": detail["eco_id"],
                                "material_uuid": variant["material_uuid"],
                                "material_id": self.resolve_material_uuid(variant["material_uuid"]),
                                "quantity": float(variant["quantity"]) if variant["quantity"] else None,
                                "unit_of_measure": variant["unit_code"],
                                "quantity_fixed": detail["quantity_fixed"],
                                "internal_id": variant["internal_id"]
                            }
                            group_record["components"].append(component)
                            total_components += 1
                        else:
                            logger.warning(f"No variants found for item {item_id}")
                    else:
                        logger.warning(f"Component {item_id} not found in details")
                
                group_record["component_count"] = len(group_record["components"])
                bom_record["groups"].append(group_record)
            
            bom_record["total_components"] = total_components
            output["boms"].append(bom_record)
        
        logger.info(f"Extracted {output['bom_count']} BOMs with {sum(b['total_components'] for b in output['boms'])} components")
        return output
    
    def to_excel(self, output_file="BOM_Extract.xlsx"):
        """Export extracted BOM to Excel (requires openpyxl)"""
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill
            
            data = self.extract_complete_bom()
            if data["status"] != "success":
                return data
            
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "BOM Extract"
            
            # Headers
            headers = ["BOM ID", "Group ID", "Line Item", "Material ID", "Quantity", "UOM", "ECO", "Active"]
            ws.append(headers)
            
            # Style header
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(start_color="CCCCCC", end_color="CCCCCC", fill_type="solid")
            
            # Data
            for bom in data["boms"]:
                for group in bom["groups"]:
                    for comp in group["components"]:
                        ws.append([
                            bom["bom_id"],
                            group["group_id"],
                            group["group_number"],
                            comp["material_id"],
                            comp["quantity"],
                            comp["unit_of_measure"],
                            comp["eco_id"],
                            "Yes" if not comp.get("end_date") or comp["end_date"].startswith("9999") else "No"
                        ])
            
            # Adjust column widths
            ws.column_dimensions['A'].width = 15
            ws.column_dimensions['B'].width = 10
            ws.column_dimensions['C'].width = 10
            ws.column_dimensions['D'].width = 20
            ws.column_dimensions['E'].width = 12
            ws.column_dimensions['F'].width = 10
            ws.column_dimensions['G'].width = 15
            ws.column_dimensions['H'].width = 10
            
            wb.save(output_file)
            logger.info(f"Exported to {output_file}")
            return {"status": "success", "file": output_file}
        
        except ImportError:
            logger.error("openpyxl not installed. Install with: pip install openpyxl")
            return {"status": "error", "message": "openpyxl required for Excel export"}


# ============ EMERGENT AGENT INTEGRATION ============

async def extract_bom_from_sap_complete(sap_instance_url, sap_username, sap_password, export_excel=False):
    """
    Complete BOM extraction from SAP BYD
    
    Args:
        sap_instance_url: e.g., "https://my431827.businessbydesign.cloud.sap"
        sap_username: SAP username
        sap_password: SAP password
        export_excel: If True, export to Excel file
    
    Returns:
        Complete BOM data with hierarchy and component details
    """
    
    extractor = SAP_BOM_Extractor(sap_instance_url, sap_username, sap_password)
    result = extractor.extract_complete_bom()
    
    if export_excel and result["status"] == "success":
        excel_result = extractor.to_excel(f"BOM_Extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        result["excel_export"] = excel_result
    
    return {
        "source": "SAP_BYD_Complete_BOM_Extract",
        "timestamp": datetime.now().isoformat(),
        "data": result
    }


# ============ USAGE EXAMPLE ============

if __name__ == "__main__":
    # Initialize
    extractor = SAP_BOM_Extractor(
        instance_url="https://my431827.businessbydesign.cloud.sap",
        username="your_username",
        password="your_password"
    )
    
    # Extract complete BOM
    result = extractor.extract_complete_bom()
    
    if result["status"] == "success":
        # Print summary
        print(f"✅ Extracted {result['bom_count']} BOMs")
        for bom in result["boms"]:
            print(f"\n📋 BOM: {bom['bom_id']}")
            for group in bom["groups"]:
                print(f"   Group {group['group_id']}: {group['component_count']} components")
                for comp in group["components"]:
                    print(f"      • {comp['material_id']}: {comp['quantity']} {comp['unit_of_measure']}")
        
        # Export to Excel
        extractor.to_excel()
    else:
        print(f"❌ Error: {result['message']}")
