using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.OS;

namespace ScadBuddy;

/// <summary>
/// The foreground service that keeps the device socket with the app behind or the screen off,
/// so flow questions notify (#8574, spec §6.3; the owner chose it on 2026-11-28). Type
/// <c>dataSync</c> (https://developer.android.com/develop/background-work/services/fgs/service-types).
/// It runs from pairing until the app is swiped away, or the pairing ends. Android 15 stops a
/// dataSync service after 6 hours in a day (OnTimeout); the app starts
/// it again the next time it comes to the front.
/// </summary>
[Service(Exported = false, ForegroundServiceType = ForegroundService.TypeDataSync)]
public sealed class TaskService : Service
{
	const int ListeningId = 8574;

	/// <summary>Starts the service, if it does not run already. Only while the app is in front: Android refuses a start from behind.</summary>
	public static void Start(Context context)
	{
		try { context.StartForegroundService(new Intent(context, typeof(TaskService))); }
		catch (Exception)
		{
			// Started from behind (ForegroundServiceStartNotAllowedException): the next resume starts it.
		}
	}

	/// <summary>Stops the service: the pairing ended.</summary>
	public static void Stop(Context context) => context.StopService(new Intent(context, typeof(TaskService)));

	public override IBinder? OnBind(Intent? intent) => null;

	public override StartCommandResult OnStartCommand(Intent? intent, StartCommandFlags flags, int startId)
	{
		TaskNotifications.Channels(this);
		var listening = TaskNotifications.Listening(this);
		if (Build.VERSION.SdkInt >= BuildVersionCodes.Q)
			StartForeground(ListeningId, listening, ForegroundService.TypeDataSync);
		else
			StartForeground(ListeningId, listening);
		TaskNotifications.Follow(this);
		DeviceLink.Instance.Hold(true);
		// Swiped away, the app is gone; it starts again when opened, not by itself.
		return StartCommandResult.NotSticky;
	}

	public override void OnTaskRemoved(Intent? rootIntent)
	{
		StopSelf();
		base.OnTaskRemoved(rootIntent);
	}

	public override void OnTimeout(int startId, ForegroundService fgsType)
	{
		StopSelf();
	}

	public override void OnDestroy()
	{
		DeviceLink.Instance.Hold(false);
		base.OnDestroy();
	}
}
