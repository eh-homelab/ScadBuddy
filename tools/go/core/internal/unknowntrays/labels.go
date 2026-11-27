package unknowntrays

import "fmt"

// external is the ams_id of the external spool holder.
const external = 255

// unitLabel is bambuddy/trays.py unit_label: AMS-A, AMS-D, HT-A, External.
func unitLabel(amsID int) string {
	switch {
	case amsID == external:
		return "External"
	case amsID >= 128:
		return fmt.Sprintf("HT-%c", rune(65+amsID-128))
	default:
		return fmt.Sprintf("AMS-%c", rune(65+amsID))
	}
}

// TrayLabel is bambuddy/trays.py tray_label: "AMS-D slot 4", the slot counted from 1.
func TrayLabel(amsID, trayID int) string {
	if amsID >= 128 && amsID != external {
		return unitLabel(amsID)
	}
	return fmt.Sprintf("%s slot %d", unitLabel(amsID), trayID+1)
}

// PlaceLabel is bambuddy/trays.py place_label: the printer and the tray label.
func PlaceLabel(printerName string, amsID, trayID int) string {
	return printerName + " " + TrayLabel(amsID, trayID)
}

// SpoolCode is a spool's code as a person reads it: S-12.
func SpoolCode(number int) string { return fmt.Sprintf("S-%d", number) }

// PlaceCode is a place's code as a person reads it: L-3.
func PlaceCode(number int) string { return fmt.Sprintf("L-%d", number) }
