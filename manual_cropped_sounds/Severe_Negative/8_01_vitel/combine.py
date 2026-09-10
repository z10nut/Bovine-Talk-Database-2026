import argparse
import csv
import os
from openpyxl import Workbook

def combine_txt_files(hfc_path, lfc_path, output_dir, input_delimiter=','):
    abs_out_dir = os.path.abspath(output_dir)
    dir_name = os.path.basename(abs_out_dir)
    
    if not dir_name:
        dir_name = "combined_output"
        
    csv_path = os.path.join(abs_out_dir, f"{dir_name}.csv")
    xlsx_path = os.path.join(abs_out_dir, f"{dir_name}.xlsx")
    
    # Initialize Excel Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Combined Data"

    with open(csv_path, mode='w', newline='', encoding='utf-8') as out_file:
        writer = csv.writer(out_file)
        
        # Process HFC file (header included)
        with open(hfc_path, mode='r', encoding='utf-8') as file1:
            reader1 = csv.reader(file1, delimiter=input_delimiter)
            for row in reader1:
                writer.writerow(row)
                ws.append(row)
                
        # Process LFC file (header skipped)
        with open(lfc_path, mode='r', encoding='utf-8') as file2:
            reader2 = csv.reader(file2, delimiter=input_delimiter)
            next(reader2, None)
            
            for row in reader2:
                writer.writerow(row)
                ws.append(row)

    # Save Excel file
    wb.save(xlsx_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Combine HFC and LFC text files into CSV and XLSX.")
    parser.add_argument("--HFC", required=True, help="Path to the HFC text file")
    parser.add_argument("--LFC", required=True, help="Path to the LFC text file")
    parser.add_argument("--out", required=True, help="Path to the output directory")
    
    args = parser.parse_args()
    
    # Ensure the output directory exists
    os.makedirs(args.out, exist_ok=True)
    
    combine_txt_files(args.HFC, args.LFC, args.out)