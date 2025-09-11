#!/usr/bin/env python3
"""
Data processor script that pulls from ServiceNow-like tables and builds reports.
This script demonstrates the issue described in the error message.
"""

import json
from typing import Dict, List, Any, Set

def pull_table_data(table: str, query: str = None, fields: int = None, limit: int = None) -> List[Dict[str, Any]]:
    """Simulate pulling data from a table."""
    print(f"[pull] table={table}", end="")
    if query:
        print(f" query='{query}'", end="")
    if fields:
        print(f" fields={fields}", end="")
    if limit:
        print(f" limit={limit}", end="")
    
    # Simulate data based on table
    if table == "incident":
        if query and "company=0aac64bf1b1d6914c843c913604bcbd3" in query:
            # Complex query returns no results
            data = []
        else:
            # Simple query returns some data
            data = [
                {
                    "sys_id": "incident_1",
                    "number": "INC0001234",
                    "short_description": "Network ALERT detected",
                    "description": "Alert from monitoring system",
                    "company": {"value": "0aac64bf1b1d6914c843c913604bcbd3", "display_value": "Verizon"},
                    "opened_at": "2025-03-15 10:00:00",
                    "state": "1"
                },
                {
                    "sys_id": "incident_2", 
                    "number": "INC0001235",
                    "short_description": "Service alert notification",
                    "description": "Alert: Service degradation",
                    "company": {"value": "0aac64bf1b1d6914c843c913604bcbd3", "display_value": "Verizon"},
                    "opened_at": "2025-03-15 11:00:00", 
                    "state": "2"
                }
            ] * 829  # Simulate 1658 rows
    elif table == "sys_user":
        if query and "email=CmAuditPRODReports@gxo.com" in query:
            # User not found
            data = []
        else:
            data = [
                {
                    "sys_id": "user_1",
                    "email": "test@example.com", 
                    "name": "Test User"
                }
            ]
    else:
        data = []
    
    print(f" rows={len(data)}")
    return data

def build_company_reports(incidents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build reports grouped by company."""
    reports = {}
    
    for incident in incidents:
        company_info = incident.get("company", {})
        
        # Extract a hashable key from company_info dict
        # Use company ID (value field) as the key, fallback to display_value or "unknown"
        if isinstance(company_info, dict):
            company_key = company_info.get("value") or company_info.get("display_value") or "unknown"
        else:
            # If company_info is already a string or other hashable type
            company_key = str(company_info) if company_info else "unknown"
        
        if company_key not in reports:
            reports[company_key] = {
                "company_info": company_info,  # Store the original company info
                "incidents": [],
                "total_count": 0,
                "alert_count": 0
            }
        
        reports[company_key]["incidents"].append(incident)
        reports[company_key]["total_count"] += 1
        
        # Check for alerts
        desc = incident.get("short_description", "") + " " + incident.get("description", "")
        if "alert" in desc.lower():
            reports[company_key]["alert_count"] += 1
    
    return reports

def main():
    """Main processing function that reproduces the error."""
    
    # Pull incident data - first query gets all incidents  
    incidents = pull_table_data("incident")
    
    # Pull user data that doesn't exist
    users = pull_table_data("sys_user", "email=CmAuditPRODReports@gxo.com", fields=2, limit=1)
    
    # Pull filtered incidents with complex query
    filtered_incidents = pull_table_data(
        "incident", 
        "company=0aac64bf1b1d6914c843c913604bcbd3^opened_at>=2025-03-15^(short_descriptionLIKEALERT^ORdescriptionLIKEALERT^ORshort_descriptionLIKEAlert^ORdescriptionLIKEAlert^ORshort_descriptionLIKEalert^ORdescriptionLIKEalert^ORshort_descriptionLIKEALERT:^ORdescriptionLIKEALERT:^ORshort_descriptionLIKEAlert:^ORdescriptionLIKEAlert:^ORshort_descriptionLIKEalert:^ORdescriptionLIKEalert:^ORshort_descriptionLIKEALERTS:^ORdescriptionLIKEALERTS:^ORshort_descriptionLIKEAlerts:^ORdescriptionLIKEAlerts:^ORshort_descriptionLIKEalerts:^ORdescriptionLIKEalerts:)",
        fields=8,
        limit=20000
    )
    
    try:
        # This should now work without the unhashable dict error
        verizon_reports = build_company_reports(incidents)
        print("Verizon reports built successfully")
        
        # Display summary of the reports
        for company_key, report_data in verizon_reports.items():
            company_name = report_data["company_info"].get("display_value", company_key)
            print(f"Company: {company_name} (ID: {company_key})")
            print(f"  Total incidents: {report_data['total_count']}")
            print(f"  Alert incidents: {report_data['alert_count']}")
            
    except Exception as e:
        print(f"Error building verizon: {e}")

if __name__ == "__main__":
    main()