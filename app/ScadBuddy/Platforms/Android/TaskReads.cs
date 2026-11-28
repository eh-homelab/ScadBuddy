using System.Text.Json.Nodes;
using System.Threading.Channels;
using Android.Nfc;

namespace ScadBuddy;

/// <summary>
/// The phone's reads for a task's scan, entity and photo fields (#8574, spec §6.4): a tag with
/// the app's NFC reader, as <c>nfc.read</c> reads it (<see cref="DeviceMethods"/>), and a photo
/// with the camera. A tag is only read here, never written. A dump holds no sector trailer
/// (<see cref="DeviceProtocol.Dump"/>).
/// </summary>
static class TaskReads
{
	/// <summary>
	/// Reads the next tag tapped and answers its tag dump, or null when the person cancelled. A
	/// MIFARE Classic read that stops partway asks for another tap and goes on from the start.
	/// </summary>
	public static async Task<string?> ReadTag(INavigation navigation)
	{
		var taps = Channel.CreateBounded<Tag>(new BoundedChannelOptions(1) { FullMode = BoundedChannelFullMode.DropWrite });
		Action<Tag> onTap = tag => taps.Writer.TryWrite(tag);
		using var cancel = new CancellationTokenSource();
		var page = new HoldPage(cancel.Cancel);
		await navigation.PushModalAsync(page, false);
		NfcBridge.Instance.StartCall(onTap);
		try
		{
			while (true)
			{
				Tag tag;
				try { tag = await taps.Reader.ReadAsync(cancel.Token); }
				catch (OperationCanceledException) { return null; }
				var progress = new Progress(page, cancel.Token);
				var reading = await Task.Run(() => NfcBridge.ReadTag(tag, progress));
				if (progress.LostError is not null)
				{
					page.Say("The tag moved. Hold it on the back of the phone again.");
					continue;
				}
				var uid = Convert.ToHexString(tag.GetId() ?? []);
				var blocks = (reading["mifare"]?["blocks"] as JsonArray ?? [])
					.Select(b => (b!["index"]!.GetValue<int>(), b["dataHex"]!.GetValue<string>()));
				return DeviceProtocol.Dump(uid, NfcBridge.TechOf(tag), blocks, reading["records"] as JsonArray ?? []);
			}
		}
		finally
		{
			NfcBridge.Instance.EndCall(onTap);
			if (navigation.ModalStack.Contains(page)) await navigation.PopModalAsync(false);
		}
	}

	/// <summary>A photo with the camera: its bytes and type, or null when none was taken. It throws with a message the person can read.</summary>
	public static async Task<(byte[] Bytes, string MediaType)?> TakePhoto(string title)
	{
		FileResult? photo;
		try { photo = await MediaPicker.Default.CapturePhotoAsync(new MediaPickerOptions { Title = title }); }
		catch (PermissionException) { throw new InvalidOperationException("The app may not use the camera. Allow it in Android's settings."); }
		catch (FeatureNotSupportedException) { throw new InvalidOperationException("This phone has no camera the app can use."); }
		if (photo is null) return null;
		await using var stream = await photo.OpenReadAsync();
		using var copy = new MemoryStream();
		var buffer = new byte[81920];
		int n;
		while ((n = await stream.ReadAsync(buffer)) > 0)
		{
			copy.Write(buffer, 0, n);
			if (copy.Length > DeviceProtocol.PhotoMax) throw new InvalidOperationException("The photo is larger than 12 MiB.");
		}
		return (copy.ToArray(), photo.ContentType ?? "image/jpeg");
	}

	sealed class Progress(HoldPage page, CancellationToken stop) : ITagProgress
	{
		public string? LostError { get; private set; }
		public bool Stopped => stop.IsCancellationRequested;
		public void Found(string uid, string tech, int blocks, int resumeFrom) => page.Say(blocks > 0 ? "Tag found. Hold it still." : "Tag found.");
		public void Read(int read, int of) => page.Say($"Read {read} of {of} blocks. Hold it still.");
		public void Lost(int read, string error) => LostError = error;
	}

	/// <summary>"Hold a tag": what to do, how it goes, and Cancel.</summary>
	sealed class HoldPage : ContentPage
	{
		readonly Action cancel;
		readonly Label status;

		public HoldPage(Action cancel)
		{
			this.cancel = cancel;
			TaskTheme.Page(this);
			status = TaskTheme.Body("Hold the tag on the back of the phone.");
			status.HorizontalTextAlignment = TextAlignment.Center;
			var stop = TaskTheme.Quiet("Cancel");
			stop.Clicked += (_, _) => cancel();
			var title = TaskTheme.Title("Hold a tag");
			title.HorizontalTextAlignment = TextAlignment.Center;
			Content = new VerticalStackLayout
			{
				Padding = new Thickness(24),
				Spacing = 28,
				VerticalOptions = LayoutOptions.Center,
				Children = { title, status, stop },
			};
		}

		public void Say(string text) => MainThread.BeginInvokeOnMainThread(() => status.Text = text);

		protected override bool OnBackButtonPressed()
		{
			cancel();
			return true;
		}
	}
}
