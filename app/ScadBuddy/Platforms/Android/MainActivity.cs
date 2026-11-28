using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.OS;

namespace ScadBuddy;

[Activity(Theme = "@style/Maui.SplashTheme", MainLauncher = true, LaunchMode = LaunchMode.SingleTop, ConfigurationChanges = ConfigChanges.ScreenSize | ConfigChanges.Orientation | ConfigChanges.UiMode | ConfigChanges.ScreenLayout | ConfigChanges.SmallestScreenSize | ConfigChanges.Density)]
[IntentFilter([Intent.ActionView], Categories = [Intent.CategoryDefault, Intent.CategoryBrowsable], DataScheme = DeepLink.Scheme, DataHost = DeepLink.Host)]
// A spool label's QR, scadbuddy:spool:<uuid>, has no host (#8550); DeepLink.SpoolOf checks it.
[IntentFilter([Intent.ActionView], Categories = [Intent.CategoryDefault, Intent.CategoryBrowsable], DataScheme = DeepLink.Scheme)]
public class MainActivity : MauiAppCompatActivity
{
	protected override void OnCreate(Bundle? savedInstanceState)
	{
		base.OnCreate(savedInstanceState);
		DeepLink.Raise(LinkOf(Intent));
		DeepLink.RaiseTask(TaskOf(Intent));
	}

	protected override void OnNewIntent(Intent? intent)
	{
		base.OnNewIntent(intent);
		DeepLink.Raise(LinkOf(intent));
		DeepLink.RaiseTask(TaskOf(intent));
	}

	protected override async void OnResume()
	{
		base.OnResume();
		// The service keeps the questions coming with the app behind (#8574): it starts from the
		// front only, so each resume starts it again after Android stopped it (its daily limit).
		if (await DeviceLink.Instance.Paired()) TaskService.Start(this);
		await TaskNotifications.AskIfWaiting();
	}

	static Uri? LinkOf(Intent? intent) =>
		intent?.Action == Intent.ActionView && Uri.TryCreate(intent.DataString, UriKind.Absolute, out var link) ? link : null;

	// A notification's tap (TaskNotifications): the task to open.
	static string? TaskOf(Intent? intent) => intent?.GetStringExtra(TaskNotifications.TaskExtra);
}
