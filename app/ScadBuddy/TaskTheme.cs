using Microsoft.Maui.Controls.Shapes;

namespace ScadBuddy;

/// <summary>
/// The task screens' look (#8574): the web app's tokens (frontend/src/index.css) where MAUI
/// allows: neutral ground, the amber primary for the one action on a screen, and the spool's own
/// colour on its card. Large touch targets: the screens are used standing at a printer or a
/// shelf, often with one hand.
/// </summary>
static class TaskTheme
{
	// Light and dark, as the web's :root and .dark.
	static readonly Color Ground = Color.FromArgb("#FFFFFF"), GroundDark = Color.FromArgb("#0A0A0A");
	static readonly Color Ink = Color.FromArgb("#0A0A0A"), InkDark = Color.FromArgb("#FAFAFA");
	static readonly Color Panel = Color.FromArgb("#F5F5F5"), PanelDark = Color.FromArgb("#171717");
	static readonly Color Muted = Color.FromArgb("#737373"), MutedDark = Color.FromArgb("#A1A1A1");
	static readonly Color Line = Color.FromArgb("#E5E5E5"), LineDark = Color.FromArgb("#2A2A2A");
	static readonly Color Amber = Color.FromArgb("#9B5D04"), AmberDark = Color.FromArgb("#F2A93B");
	static readonly Color OnAmber = Color.FromArgb("#FFF8EC"), OnAmberDark = Color.FromArgb("#1A1204");
	static readonly Color Alert = Color.FromArgb("#C70009"), AlertDark = Color.FromArgb("#FF6467");

	public const double Touch = 52;

	public static void Page(ContentPage page) => page.SetAppThemeColor(VisualElement.BackgroundColorProperty, Ground, GroundDark);

	public static Label Title(string text)
	{
		var label = new Label { Text = text, FontSize = 24, FontFamily = "OpenSansSemibold", LineBreakMode = LineBreakMode.WordWrap };
		label.SetAppThemeColor(Label.TextColorProperty, Ink, InkDark);
		return label;
	}

	public static Label Body(string text, double size = 16)
	{
		var label = new Label { Text = text, FontSize = size, LineBreakMode = LineBreakMode.WordWrap };
		label.SetAppThemeColor(Label.TextColorProperty, Ink, InkDark);
		return label;
	}

	public static Label FieldLabel(string text)
	{
		var label = new Label { Text = text, FontSize = 15, FontFamily = "OpenSansSemibold", LineBreakMode = LineBreakMode.WordWrap };
		label.SetAppThemeColor(Label.TextColorProperty, Ink, InkDark);
		return label;
	}

	public static Label Small(string text)
	{
		var label = new Label { Text = text, FontSize = 13, LineBreakMode = LineBreakMode.WordWrap };
		label.SetAppThemeColor(Label.TextColorProperty, Muted, MutedDark);
		return label;
	}

	public static Label Problem()
	{
		var label = new Label { FontSize = 13, IsVisible = false, LineBreakMode = LineBreakMode.WordWrap };
		label.SetAppThemeColor(Label.TextColorProperty, Alert, AlertDark);
		return label;
	}

	/// <summary>The screen's one action: full width, amber.</summary>
	public static Button Primary(string text)
	{
		var button = new Button { Text = text, FontSize = 18, FontFamily = "OpenSansSemibold", MinimumHeightRequest = 60, CornerRadius = 10 };
		button.SetAppThemeColor(VisualElement.BackgroundColorProperty, Amber, AmberDark);
		button.SetAppThemeColor(Button.TextColorProperty, OnAmber, OnAmberDark);
		return button;
	}

	/// <summary>A second action: outlined, as tall as a finger.</summary>
	public static Button Quiet(string text)
	{
		var button = new Button { Text = text, FontSize = 16, MinimumHeightRequest = Touch, CornerRadius = 10, BorderWidth = 1, BackgroundColor = Colors.Transparent };
		button.SetAppThemeColor(Button.TextColorProperty, Ink, InkDark);
		button.SetAppThemeColor(Button.BorderColorProperty, Line, LineDark);
		return button;
	}

	/// <summary>A choice's button, marked when chosen.</summary>
	public static void Chosen(Button button, bool chosen)
	{
		if (chosen)
		{
			button.SetAppThemeColor(VisualElement.BackgroundColorProperty, Amber, AmberDark);
			button.SetAppThemeColor(Button.TextColorProperty, OnAmber, OnAmberDark);
			button.SetAppThemeColor(Button.BorderColorProperty, Amber, AmberDark);
		}
		else
		{
			button.BackgroundColor = Colors.Transparent;
			button.SetAppThemeColor(Button.TextColorProperty, Ink, InkDark);
			button.SetAppThemeColor(Button.BorderColorProperty, Line, LineDark);
		}
	}

	public static Entry Entry(Keyboard keyboard, string? placeholder = null)
	{
		var entry = new Entry { Keyboard = keyboard, FontSize = 18, MinimumHeightRequest = Touch, Placeholder = placeholder };
		entry.SetAppThemeColor(Microsoft.Maui.Controls.Entry.TextColorProperty, Ink, InkDark);
		entry.SetAppThemeColor(Microsoft.Maui.Controls.Entry.PlaceholderColorProperty, Muted, MutedDark);
		return entry;
	}

	/// <summary>A quiet panel: the context and a looked-up code sit in one.</summary>
	public static Border Card(View content, double padding = 14)
	{
		var border = new Border { Content = content, Padding = new Thickness(padding), StrokeThickness = 1, StrokeShape = new RoundRectangle { CornerRadius = 10 } };
		border.SetAppThemeColor(VisualElement.BackgroundColorProperty, Panel, PanelDark);
		border.SetAppThemeColor(Border.StrokeProperty, Line, LineDark);
		return border;
	}

	/// <summary>A spool's colour as a round swatch, from <c>#RRGGBBAA</c>; null when the text is no colour.</summary>
	public static View? Swatch(string? rgba, double size = 40)
	{
		if (rgba is not { Length: 9 } || rgba[0] != '#') return null;
		// The web writes RRGGBBAA; MAUI reads #AARRGGBB.
		if (!Color.TryParse("#" + rgba[7..9] + rgba[1..7], out var colour)) return null;
		var dot = new Ellipse { Fill = new SolidColorBrush(colour), WidthRequest = size, HeightRequest = size, StrokeThickness = 1, VerticalOptions = LayoutOptions.Center };
		dot.SetAppThemeColor(Shape.StrokeProperty, Line, LineDark);
		return dot;
	}
}
