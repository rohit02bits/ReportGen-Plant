import os
import sys
import logging
from datetime import datetime
import pandas as pd
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger("ReportGenerator.ReportGenerator")

class ReportGenerator:
    def __init__(self, config):
        self.config = config
        self.reporting_config = config.get("reporting", {})
        output_dir_raw = self.reporting_config.get("output_dir", "./reports")
        if not os.path.isabs(output_dir_raw):
            base_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
            self.output_dir = os.path.join(base_dir, output_dir_raw)
        else:
            self.output_dir = output_dir_raw
            
        self.template_title = self.reporting_config.get("template_title", "BATCHING REPORT (SHIFT WISE)")
        self.header_row_idx = self.reporting_config.get("header_row", 5)
        self.data_start_row = self.reporting_config.get("data_start_row", 6)
        
        # Ensure output directory exists
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
            logger.info(f"Created output directory: {self.output_dir}")

    def generate_excel_report(self, shift_name: str, start_dt: datetime, end_dt: datetime, df: pd.DataFrame):
        """
        Generates and styles the Excel report for the given shift and dataset.
        """
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Batching Report"
        
        # Enable grid lines explicitly
        if self.reporting_config.get("excel_gridlines", True):
            ws.views.sheetView[0].showGridLines = True

        # Styles definition (Arial font, size 10 default, matching the screenshot)
        font_family = "Arial"
        font_title = Font(name=font_family, size=12, bold=True)
        font_meta = Font(name=font_family, size=10, bold=True)
        font_header = Font(name=font_family, size=10, bold=True)
        font_data = Font(name=font_family, size=10)
        font_summary = Font(name=font_family, size=10, bold=True)
        
        align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        align_right = Alignment(horizontal="right", vertical="center")
        align_left = Alignment(horizontal="left", vertical="center")
        
        fill_header = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
        fill_summary = PatternFill(start_color="EAEAEA", end_color="EAEAEA", fill_type="solid")
        
        thin_border_side = Side(style='thin', color='D3D3D3')
        double_border_side = Side(style='double', color='000000')
        border_all = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
        
        # Borders for headers (thin top/bottom)
        border_header = Border(
            left=thin_border_side, right=thin_border_side, 
            top=Side(style='thin', color='000000'), 
            bottom=Side(style='thin', color='000000')
        )
        # Borders for summary row (thin top, double bottom)
        border_summary = Border(
            left=thin_border_side, right=thin_border_side,
            top=Side(style='thin', color='000000'),
            bottom=double_border_side
        )

        # 1. Title block (Row 3)
        ws.row_dimensions[3].height = 24
        # Place title at Column D
        ws["D3"] = self.template_title
        ws["D3"].font = font_title
        ws["D3"].alignment = align_left

        # Report Date (Use start datetime's date, formatted as DD-MM-YY)
        report_date_str = start_dt.strftime("%d-%m-%y")
        # Write Date to R3
        ws["R3"] = report_date_str
        ws["R3"].font = font_meta
        ws["R3"].alignment = align_center

        # Shift Name (e.g., "SHIFT A")
        ws["T3"] = shift_name.upper()
        ws["T3"].font = font_meta
        ws["T3"].alignment = align_center

        # 2. Write Headers (Row 5)
        ws.row_dimensions[self.header_row_idx].height = 28
        columns_config = self.config.get("columns", [])
        
        for col_def in columns_config:
            col_letter = col_def["excel_col"]
            header_text = col_def["excel_header"]
            cell = ws[f"{col_letter}{self.header_row_idx}"]
            cell.value = header_text
            cell.font = font_header
            cell.alignment = align_center
            cell.fill = fill_header
            cell.border = border_header

        # 3. Write Data Rows (Row 6 onwards)
        current_row = self.data_start_row
        
        # If DataFrame is empty, write a placeholder message
        if df.empty:
            logger.warning("No data found to write to report.")
            ws.row_dimensions[current_row].height = 20
            # Merge some cells to show "NO DATA AVAILABLE"
            ws[f"D{current_row}"] = "NO DATA AVAILABLE FOR THIS TIME PERIOD"
            ws[f"D{current_row}"].font = Font(name=font_family, size=10, italic=True)
            ws[f"D{current_row}"].alignment = align_left
            current_row += 1
        else:
            # We iterate through the records and map to their Excel columns
            for idx, row in df.iterrows():
                ws.row_dimensions[current_row].height = 18
                
                for col_def in columns_config:
                    col_letter = col_def["excel_col"]
                    db_field = col_def["db_field"]
                    val_type = col_def.get("type", "string")
                    num_format = col_def.get("format")
                    
                    cell = ws[f"{col_letter}{current_row}"]
                    cell.font = font_data
                    cell.border = border_all
                    
                    # Fetch value from row
                    if db_field in row:
                        val = row[db_field]
                        
                        if pd.isna(val):
                            cell.value = ""
                        elif val_type == "int":
                            cell.value = int(val)
                            cell.alignment = align_center
                            if num_format:
                                cell.number_format = num_format
                        elif val_type == "float":
                            cell.value = float(val)
                            cell.alignment = align_right
                            if num_format:
                                cell.number_format = num_format
                        elif val_type == "datetime":
                            if isinstance(val, str):
                                try:
                                    val = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                                except ValueError:
                                    pass
                            
                            if isinstance(val, datetime):
                                # Format date like YY-MM-DD-HH:MM:SS
                                # For Excel to format it, we write it as a datetime object
                                cell.value = val
                                # Custom Excel datetime format string
                                cell.number_format = "DD-MM-YY-HH:MM:SS"
                            else:
                                cell.value = str(val)
                            cell.alignment = align_center
                        else:
                            cell.value = str(val)
                            cell.alignment = align_left
                    else:
                        cell.value = ""
                        
                current_row += 1

        # 4. Write Summary Row (Average for float columns)
        if not df.empty:
            summary_row = current_row
            ws.row_dimensions[summary_row].height = 20
            
            # Label the summary row. Let's put "AVERAGE" in Column D (where Batching Time is)
            ws[f"D{summary_row}"] = "AVERAGE"
            ws[f"D{summary_row}"].font = font_summary
            ws[f"D{summary_row}"].alignment = align_center
            ws[f"D{summary_row}"].border = border_summary
            ws[f"D{summary_row}"].fill = fill_summary
            
            # Set summary formulas/values for columns
            for col_def in columns_config:
                col_letter = col_def["excel_col"]
                # Skip Column B and Column D which are Batch No and Batching Time
                if col_letter in ["B", "D"]:
                    # Ensure summary border is still applied to B
                    if col_letter == "B":
                        ws[f"B{summary_row}"].border = border_summary
                        ws[f"B{summary_row}"].fill = fill_summary
                    continue
                
                cell = ws[f"{col_letter}{summary_row}"]
                cell.border = border_summary
                cell.fill = fill_summary
                
                # Check if this column needs an average summary
                if col_def.get("avg_summary", False):
                    # Write Excel AVERAGE formula: =AVERAGE(ColStart:ColEnd)
                    start_cell = f"{col_letter}{self.data_start_row}"
                    end_cell = f"{col_letter}{summary_row - 1}"
                    cell.value = f"=AVERAGE({start_cell}:{end_cell})"
                    cell.font = font_summary
                    cell.alignment = align_right
                    if col_def.get("format"):
                        cell.number_format = col_def["format"]

        # 5. Set column widths (including narrow spacers)
        # First set spacers to narrow width
        data_cols = [c["excel_col"] for c in columns_config]
        all_cols_in_range = []
        
        # Determine the maximum column used
        max_col_letter = max(data_cols)
        max_col_idx = openpyxl.utils.column_index_from_string(max_col_letter)
        
        for c_idx in range(1, max_col_idx + 2):
            c_letter = get_column_letter(c_idx)
            if c_letter in data_cols:
                # Wide column for data
                if c_letter == "B":
                    ws.column_dimensions[c_letter].width = 12 # Batch No
                elif c_letter == "D":
                    ws.column_dimensions[c_letter].width = 24 # Batching Time
                else:
                    ws.column_dimensions[c_letter].width = 14 # Values
            else:
                # Narrow column for spacers
                ws.column_dimensions[c_letter].width = 3

        # Write output file
        # Format filename: BatchingReport_ShiftA_20260816.xlsx
        date_stamp = start_dt.strftime("%Y%m%d")
        safe_shift_name = shift_name.replace(" ", "_")
        filename = f"BatchingReport_{safe_shift_name}_{date_stamp}.xlsx"
        filepath = os.path.join(self.output_dir, filename)
        
        wb.save(filepath)
        logger.info(f"Report generated successfully: {filepath}")
        return filepath
