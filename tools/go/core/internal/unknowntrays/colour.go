package unknowntrays

import (
	"math"
	"regexp"
	"strconv"
	"strings"
)

var colourPattern = regexp.MustCompile(`^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$`)

// normaliseColour is bambuddy/filaments.py normalise_colour: RRGGBBAA, #RRGGBBAA and
// #RRGGBB all give #RRGGBB, and anything else gives "".
func normaliseColour(raw *string) string {
	if raw == nil {
		return ""
	}
	text := strings.TrimLeft(strings.TrimSpace(*raw), "#")
	if !colourPattern.MatchString(text) {
		return ""
	}
	return "#" + strings.ToUpper(text[:6])
}

type lab struct{ l, a, b float64 }

// toLab is `_lab`: an sRGB colour in CIE L*a*b*, against the D65 white.
func toLab(colour string) lab {
	channel := func(i int) float64 {
		v, _ := strconv.ParseUint(colour[i:i+2], 16, 8)
		c := float64(v) / 255
		if c <= 0.04045 {
			return c / 12.92
		}
		return math.Pow((c+0.055)/1.055, 2.4)
	}
	red, green, blue := channel(1), channel(3), channel(5)
	x := (0.4124564*red + 0.3575761*green + 0.1804375*blue) / 0.95047
	y := 0.2126729*red + 0.7151522*green + 0.0721750*blue
	z := (0.0193339*red + 0.1191920*green + 0.9503041*blue) / 1.08883
	f := func(t float64) float64 {
		if t > 216.0/24389.0 {
			return math.Cbrt(t)
		}
		return (24389.0/27.0*t + 16) / 116
	}
	fx, fy, fz := f(x), f(y), f(z)
	return lab{116*fy - 16, 500 * (fx - fy), 200 * (fy - fz)}
}

func radians(degrees float64) float64 { return degrees * math.Pi / 180 }

// hueOf is atan2 in degrees, taken mod 360 as Python's `%` takes it: never negative.
func hueOf(b, a float64) float64 {
	h := math.Mod(math.Atan2(b, a)*180/math.Pi, 360)
	if h < 0 {
		h += 360
	}
	return h
}

// ciede2000 is `ciede2000`: the CIEDE2000 difference (Sharma, Wu and Dalal, 2005) with
// kL = kC = kH = 1.
func ciede2000(first, second lab) float64 {
	l1, a1, b1 := first.l, first.a, first.b
	l2, a2, b2 := second.l, second.a, second.b
	cMean := (math.Hypot(a1, b1) + math.Hypot(a2, b2)) / 2
	pow25 := math.Pow(25, 7)
	g := 0.5 * (1 - math.Sqrt(math.Pow(cMean, 7)/(math.Pow(cMean, 7)+pow25)))
	a1p, a2p := a1*(1+g), a2*(1+g)
	c1p, c2p := math.Hypot(a1p, b1), math.Hypot(a2p, b2)
	var h1p, h2p float64
	if c1p != 0 {
		h1p = hueOf(b1, a1p)
	}
	if c2p != 0 {
		h2p = hueOf(b2, a2p)
	}

	deltaL := l2 - l1
	deltaC := c2p - c1p
	var deltaH float64
	switch {
	case c1p*c2p == 0:
	case math.Abs(h2p-h1p) <= 180:
		deltaH = h2p - h1p
	case h2p-h1p > 180:
		deltaH = h2p - h1p - 360
	default:
		deltaH = h2p - h1p + 360
	}
	deltaBigH := 2 * math.Sqrt(c1p*c2p) * math.Sin(radians(deltaH/2))

	lMean := (l1 + l2) / 2
	cpMean := (c1p + c2p) / 2
	var hMean float64
	switch {
	case c1p*c2p == 0:
		hMean = h1p + h2p
	case math.Abs(h1p-h2p) <= 180:
		hMean = (h1p + h2p) / 2
	case h1p+h2p < 360:
		hMean = (h1p + h2p + 360) / 2
	default:
		hMean = (h1p + h2p - 360) / 2
	}
	t := 1 -
		0.17*math.Cos(radians(hMean-30)) +
		0.24*math.Cos(radians(2*hMean)) +
		0.32*math.Cos(radians(3*hMean+6)) -
		0.20*math.Cos(radians(4*hMean-63))
	deltaTheta := 30 * math.Exp(-math.Pow((hMean-275)/25, 2))
	cp7 := math.Pow(cpMean, 7)
	rC := 2 * math.Sqrt(cp7/(cp7+pow25))
	sL := 1 + 0.015*math.Pow(lMean-50, 2)/math.Sqrt(20+math.Pow(lMean-50, 2))
	sC := 1 + 0.045*cpMean
	sH := 1 + 0.015*cpMean*t
	rT := -math.Sin(radians(2*deltaTheta)) * rC
	lightness, chroma, hue := deltaL/sL, deltaC/sC, deltaBigH/sH
	return math.Sqrt(lightness*lightness + chroma*chroma + hue*hue + rT*chroma*hue)
}

// ColourDistance is bambuddy/filaments.py colour_distance: the CIEDE2000 difference of two
// sRGB colours with the alpha dropped, nil when a side has no colour. Nil is no match,
// never a distance of 0.
func ColourDistance(left, right *string) *float64 {
	first, second := normaliseColour(left), normaliseColour(right)
	if first == "" || second == "" {
		return nil
	}
	d := ciede2000(toLab(first), toLab(second))
	return &d
}
